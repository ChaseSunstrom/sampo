---
name: ui-design
description: "Use whenever you build or restyle a page, component, animation or 3D scene: direction, tokens, motion, QA."
---

# Sampo: UI design, motion and 3D

One path for any work on how a screen looks or moves: a web page, an app screen, a component, an animation, a 3D
scene, or "make it look good". It routes to the UI playbooks (paths below are relative to this skill; the same files
print with `sampo playbooks show ui/NAME`) and to the sampo verbs that measure the result. The aim is a UI with a point of
view, never the generic card kit: every step below either sets a decision down or proves one. A terminal UI or a
Claude Code mod takes `../playbooks/references/ui/terminal-ui.md` instead.

Work inside a focused task (each step edits files). Per step, read only the playbook it names.

## 1. Direction: decide what it should feel like

- The repo already has one (a `DESIGN.md` with a `## Direction` section, a brand guide, a shipped design system):
  it wins. Read it and go to step 2, unless the user asked for a new look.
- No direction, or the user wants looks or styles to choose from: load: Skill sampo:directions (six one-screen
  cards from one sentence, the pick saved as `DESIGN.md` with an audacity level).
- Show real specimens instead of describing them: `sampo page looks` writes a gallery with one living specimen per look
  card; `sampo looks` ranks the twenty by the user's past keeps and reverts, so offer from its top.
- The stance, the taste words, the direction bank and "Defaults to avoid":
  `../playbooks/references/ui/design-direction.md`.

## 2. Tokens: type, colour, space

- `../playbooks/references/ui/type-and-colour.md`: type scale and pairing, OKLCH palette roles, dark mode as its own
  pass.
- Existing CSS: `sampo design extract SRC` writes the token table from its custom properties into DESIGN.md's
  `## Tokens` section (`--out -` prints it instead). From nothing:
  `sampo design palette SEED --out tokens.css` makes a light and dark palette from one colour (`--format css|dtcg|tailwind`);
  it prints to stdout, so redirect with >. Tokens from Figma or a DTCG `.json`:
  `sampo design tokens tokens.json --to md --out DESIGN.md`. Hardcoded values: `sampo design infer SRC` proposes tokens.
- Brand assets: `sampo design tokens DESIGN.md --to brand --out public/` writes favicon.svg and card.svg.
- Token drift: `sampo design xray PAGE --out xray.png` outlines each element green (token colour) or red.
- Prove it before building on it: `sampo design contrast TOKENS` checks the role pairs (body text 7:1 in dark).

## 3. Build: layout and behaviour

- Start from a strict template: `sampo new list`, then `sampo new web DIR` (a page) or `sampo new scene DIR` (a 3D scene);
  `sampo new check PATH` keeps it strict.
- `../playbooks/references/ui/composition.md`: rhythm, grids, depth from one light, the hero, a state atlas.
- `../playbooks/references/ui/interaction.md`: feedback deadlines, keyboard walk, form errors, 360 px touch rules.
- Charts: `../playbooks/references/ui/data-viz.md`. Story pages: `../playbooks/references/ui/scrollytelling.md`.
- Code-side cards: `sampo lib show NAME` (the playbooks name which ones).

## 4. Motion: designed, not sprinkled

- `../playbooks/references/ui/motion.md`: timing tokens, a state-to-motion table, storyboards, reduced motion as its
  own path.
- Write the Motion Score (`motion.toml` at the repo root), then `sampo motion check` (a missing reduced form, an unknown
  easing, a token DESIGN.md lacks) and `sampo motion compile --to css|waapi|three|lab --out FILE` to emit the code.
- Games and playful UI: `../playbooks/references/ui/game-feel.md`. Sound: `../playbooks/references/ui/sound-design.md`.

## 5. 3D: a scene that never breaks the page

- `../playbooks/references/ui/scene-3d.md`: composition, light and material, a frame budget, the survival contract
  (no WebGL, reduced motion, lost context), black-screen triage.
- Describe the scene once as data (camera, lights, materials, meshes, animations) in `scene.json`, then
  `sampo scene compile scene.json --to three --out scene.js` (a three.js module) or `--to component` (a component).
- No dependencies: the one-file WebGL2 template `../playbooks/references/ui/templates/scene.html` (`sampo new scene`).
- Light rigs and PBR presets: `sampo lib show light-and-material`; a shader texture for one surface, never the whole
  direction: `sampo lib list --area looks`, then `sampo lib show shader-NAME`; libraries: `sampo lib show three-js`,
  `sampo lib show react-three-fiber`.

## 6. Check: prove it by looking

- `sampo design lint SRC`: generic-design tells and craft slips; fix each or say why it stays. Its last line names
  DESIGN.md tokens no file uses: wire them in or drop them from the table.
- `sampo design check`: DESIGN.md's opted-in gates (`## Gates`, `- brief: advisory|blocking`): the Knobs' colours, type families and durations, found in the code; `- a11y: …`: token contrast pairs, a reduced-motion path, no fast flashes, img alt and a focus style that shows; `- debt: advisory 40`: untokened colours, durations and radii per file, under the budget.
- `sampo design contrast TOKENS` again after any colour change; `sampo motion check` after any motion change.
- `sampo critique URL`: the styles the page really uses against DESIGN.md's tokens (unnamed, near-duplicate, one-off); `--why SELECTOR` shows which rule won. `--bingo` scores nine tells of a templated look (exit 1 on a full line); `--marks shot.png` turns pencil-red strokes drawn on a screenshot into one captured FIX per marked element (`--note` says what each asks); `--brief [ID]` checks that the phrases the brief quotes are on the page.
- `../playbooks/references/ui/visual-qa.md`: `sampo smoke` shoots the frame matrix (360/768/1440, light and dark,
  reduced motion); Read the PNGs and look. Before and after: `sampo img diff A.png B.png --out diff.png`.
- A component's states (default, hover, error, empty, loading): `sampo stories CHART` shoots each into one matrix page. `sampo shot` and `sampo stories` take `--profile tv|eink|car|wrist` (that surface's size and scale) and `--reduced-motion` (prefers-reduced-motion: reduce, for posters and still states).
- Record each as evidence on the task's step; a UI change isn't done on green tests alone.

## 7. Taste

The user keeps or reverts a look: `sampo looks keep LOOK` or `sampo looks revert LOOK`, so the next ranking learns it.
