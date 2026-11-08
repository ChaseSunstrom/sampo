<!-- Sampo's own playbook (written for Sampo, not ported). -->
# Design direction before pixels

Use before building or restyling any web or app screen that people will look at: a new page, a new product, a
"make it look better", a "make it pop". Left alone, generated UI drifts to the same median look (centred hero,
indigo accent, 12 px cards, one sans). This playbook makes you pick a point of view, write it down, and hold the
build to it. For terminals and Claude Code mods use `terminal-ui.md` instead.

Built on two library cards: `sampo lib show design-systems` (turn the knobs below into layered tokens) and
`sampo lib show typography` (rem sizing, fluid scale, font loading). This file decides *what* the UI should be;
those cards cover *how* to ship it.

## The procedure

1. **Look for a direction that already exists.** A `DESIGN.md` with a `## Direction` section, a brand guide or a
   shipped design system wins over everything below. A token file, or a `DESIGN.md` holding only the Tokens section
   `sampo design extract` writes, is a palette, not a direction: keep its values unless the direction you pick replaces
   them. Pass: you name the file you followed, or say "none found".
2. **Get the taste words.** Collect the adjectives the user used. If there are none, or only the empty ones
   (see the table), run the interview.
3. **Translate words to knobs** with the taste-word table. Five knobs: type, colour, density, motion, radius.
4. **Pick one direction from the bank**, plus a runner-up you reject in writing. You may blend two at most:
   one owns type, the other owns colour. Never three. To offer the user a choice first, `/sampo:directions`
   drafts six distinct directions from one sentence and saves the pick through steps 5 and 6.
5. **Set the audacity dial**: safe, bold or wild.
6. **Write `DESIGN.md` before the first component.** Pass: the file exists in the diff and predates (or shares
   a commit with) the first styled component.
7. **Build, then check** against "Defaults to avoid" and the Checks list. A failed check is a defect, not taste.

## Interview

Three questions, asked once, in one prompt, each with your proposed answer first so a busy user can just accept.
They are about the world the screen lives in, not about UI, because people describe feelings better than layouts.

1. **"Who opens this, and what were they doing a minute before?"** Sets density and pace. A nurse between
   patients needs Instrument; someone on a sofa browsing ceramics can take Gallery.
2. **"Name a physical thing, not a website, it should feel like."** A receipt, a field notebook, a
   cockpit, a concert poster. This picks the row in the direction bank. Websites as answers drag you back to
   the median; push once for an object.
3. **"What must it never be mistaken for?"** The anti-reference. "Not a bank", "not a kids' app", "not
   another SaaS dashboard". It becomes the Banned list in `DESIGN.md`.

Under full autonomy, answer the three yourself from the repo (README, audience, copy, existing assets), write the
answers into `DESIGN.md` marked *assumed*, and carry on. Pass: every answer is either quoted from the user or
marked assumed.

## Taste words

Each row sets all five knobs. Numbers are starting points; the checks hold you to the direction, not the digit.

| Word | Type | Colour | Density | Motion | Radius |
|---|---|---|---|---|---|
| premium | fine-weight display or high-contrast serif; scale ratio ≥ 1.333; small caps labels tracked +0.08em | near-monochrome plus one muted accent, saturation ≤ 40% | sparse: section padding ≥ 96 px on desktop | slow fades 300–500 ms, ease-out, no overshoot | 0–4 px |
| playful | rounded or quirky display at 700–900 weight | 3–5 saturated hues used as fills, not tints | medium | spring with overshoot, 200–350 ms | ≥ 16 px or full pill |
| calm | humanist sans, ratio 1.2, line height 1.6 | low-saturation tints; contrast lives in the text, not the surfaces | airy: 8 px unit, gaps ≥ 24 px | 200–300 ms, opacity and small translate only | 8–12 px |
| trustworthy | sturdy sans, two weights only (regular, semibold) | one deep ink (navy, forest, oxblood) plus neutrals; red reserved for errors | medium | ≤ 150 ms, functional only | 4–6 px |
| technical | grotesk for text, mono or tabular figures for every number | neutral base; colour only for state (ok, warn, error, selected) | dense: 4 px unit, rows 28–32 px | ≤ 120 ms, state changes only | 2–4 px |
| bold | condensed or black-weight display at 4–8× body size | two colours at full saturation in large fields | contrasting: dense blocks beside huge type | 150–250 ms, hard cuts allowed | 0 or full pill, nothing between |
| friendly | rounded sans, ratio 1.25 | warm accent (coral, marigold, tomato) on warm neutrals | medium | 200–300 ms ease-out | 10–16 px |
| editorial | serif for text, real italics, measure 60–70ch | ink, paper and one accent | text-led, asymmetric columns | almost none | 0 |
| futuristic | extended sans or mono, uppercase labels | dark base, one luminous accent, thin 1 px lines | medium-dense | staggered reveals 150–250 ms | 0–2 px or chamfered corners |

Two words that pull apart ("premium but playful"): the first word owns type and colour; the second gets motion
and one signature move. Write that split into `DESIGN.md`.

**Empty words.** "Modern", "clean", "sleek", "nice", "professional", "minimal" describe the median, so they set no
knob. Treat a request made only of them as having no taste words: run the interview. **"Retro"** is half a word:
ask which decade and which medium (1970s paperback, 1990s arcade, 2000s glossy web) before mapping it.

## Direction bank

Twelve points of view that do not resemble each other. Pick by matching interview answer 2 to "Feels like".

| Direction | Feels like | Type | Colour | Shape and density | Motion | Signature move |
|---|---|---|---|---|---|---|
| Broadsheet | a weekend newspaper | serif display and serif text, ratio 1.414 | ink #141414 on paper #F6F2EA, one red #B3261E | 12-column grid, 1 px hairline rules, radius 0 | none past page load | column rules between sections; a drop cap on long reads |
| Instrument | a mixing desk | grotesk with tabular figures, body 13–14 px | graphite surfaces; colour means state | 4 px unit, 28 px rows, radius 2 | ≤ 100 ms, state only | every number right-aligned on tabular figures; inline sparklines |
| Gallery | a white-walled exhibition | large light display; wide-tracked labels | bone #F4F1EC, charcoal #222, one muted brass #9A8866 | one work per viewport, huge margins | slow 400–500 ms fades (motion.md's 500 ms ceiling holds) | captions set as small museum labels beside the work |
| Toybox | a tub of building bricks | rounded display at 800+ | four primaries on cream, as solid fills | chunky; 3 px dark outlines; hard offset shadow 4 px 4 px 0 | spring overshoot | buttons that press in: shadow shrinks, face moves 2 px |
| Raw | a printed spec sheet | system UI or mono at one size, bold for headings | black, white, link blue #0000EE | 2 px black borders, radius 0, no shadows | none | structure left visible: borders, underlined links, plain tables |
| Blueprint | an engineering drawing | mono uppercase labels, condensed sans text | navy #0B2545 field, cyan and white lines | dotted grid background, dimension lines | lines draw in over 300 ms | annotations with leader lines pointing at parts of the UI |
| Field guide | a naturalist's notebook | humanist serif, italic for names | moss, ochre and bark on cream | wide margins carrying notes, radius 6 | gentle, ≤ 250 ms | margin notes and small specimen drawings beside content |
| Civic | a well-run public form | plain sans, body 19 px, bold questions as headings | black on white, one strong blue, yellow focus ring | single column, targets ≥ 44 px, radius 0 | none | oversized focus states; every heading is a question |
| Night arcade | a cabinet at 2 a.m. | extended or pixel display, mono UI | near-black #0A0A12, two neon accents | chamfered corners, medium density | snappy 80–150 ms, capped shake on impact | glow on the active element only, nowhere else |
| Riso zine | a two-colour risograph print | condensed bold display at mixed sizes | two spot inks, fluoro pink #FF48B0 and blue #0078BF, overprinting off-white | collage; pieces rotated ±3° | stepped, never smooth | misregistration: headline repeated in the second ink, offset 2 px |
| Soft utility | a well-made kitchen appliance | humanist sans, ratio 1.2 | soft neutrals, one tint per section | 8 px unit, radius 12, low shadows | 200 ms opacity and transform | everything one tap away; hit areas larger than they look |
| Poster | a mid-century concert poster | one grotesk at extreme sizes, display ≥ 8× body | white, black, one red | asymmetric grid, flush left ragged right, radius 0 | hard cuts | one headline set so large it bleeds off the edge |

A direction is a starting stance, not a costume. Keep its signature move and its knobs; adapt hex values to the
brand. Pass: `DESIGN.md` names the direction, the runner-up, and why the runner-up lost.

**More points of view: the style shelf.** Twenty further looks (lab notebook, transit signage, cartographer, ceramic
studio, broadcast graphics, archive ledger, night market, card catalogue, sewing pattern, shipping manifest,
observatory, lido, swiss poster, darkroom, botanical press, letterpress, cinema lobby, glacier, saltpan, pit stop) are library cards with measured palettes, a contrast test and greps to check the build:
`sampo lib list --area looks`, then `sampo lib show look-NAME`. They count as bank rows for steps 4 and 5.

## Audacity

One dial, three settings. It controls how far the knobs move from neutral, never the accessibility floor.

| Setting | Knobs off neutral | Signature moves | Type scale ratio | Layout | Default for |
|---|---|---|---|---|---|
| safe | 1–2 of 5 | 1 | ≤ 1.25 | standard grid, no breaks | settings, admin, forms, a screen inside an existing product |
| bold | 3–4 of 5 | 2 | 1.333–1.5 | one section breaks the grid on purpose | landing pages, onboarding, a new product's home |
| wild | 5 of 5 | 3 | ≥ 1.618, or mixed scales | grid broken on purpose, overlap allowed | games, portfolios, events, one-off launches |

Turn it to wild only when the surface is one of those or the user says so ("weird", "loud", "art", "nothing like
the others"). When unsure, pick one step lower and offer the next step up in your report.

When a taste word and the dial disagree (premium asks for a type ratio ≥ 1.333, safe caps it at 1.25), the
user's word wins on the knob it names and the dial governs the rest. Motion values from either table are inputs to
`motion.md`'s tokens: its 500 ms ceiling for interactive motion and its spring rule always hold.

**Floors no setting moves:** body text contrast ≥ 4.5:1 and large text ≥ 3:1; a visible focus indicator on every
interactive element; `prefers-reduced-motion` honoured (motion drops to opacity or none); targets ≥ 24×24 CSS px;
text is real text, not images; the layout survives 200% zoom.

## Defaults to avoid

The median look. Each line is a fail unless `DESIGN.md` names it and gives a reason.

1. **Indigo or violet accent** (the #6366F1 / #8B5CF6 family) or a purple-to-blue gradient.
2. **A typeface nobody chose.** Inter, Roboto, Arial or the system stack as the only family with no stated reason.
3. **The stock landing page:** centred headline, a subline, two equal buttons, then three icon cards in a row.
4. **One radius and one shadow everywhere.** Every surface at 8–12 px with the same soft shadow.
5. **Emoji as icons,** and a sparkle standing for "AI".
6. **Pale grey body text** below 4.5:1 on its background.
7. **Frosted glass over blurred gradient blobs.**
8. **Flat hierarchy:** adjacent heading levels less than 1.2× apart, so weight alone does the work.
9. **Even spacing:** the gap inside a group equals the gap between groups.
10. **Filler voice in headings:** revolutionize, seamless, unlock, supercharge, elevate, effortless.
11. **Dark mode with a neon accent** nobody asked for (Night arcade is a choice, not a default).
12. **Everything fades up on scroll** with the same delay.
13. **Two equal calls to action.** "Get started" and "Learn more" at the same weight: one must lead.

**The cover test.** Hide the logo and product name in a screenshot. If the screen could belong to any competitor,
the direction did not reach the pixels: fail, and push the signature move harder.

## DESIGN.md

Save at the repo root as `DESIGN.md` (or next to an existing design-system package). Read it before every UI
change. Change it only on purpose, with the reason in the task log; never let a component quietly disagree.

```markdown
# DESIGN.md: <product>

## Point of view
It should feel like <physical thing>, for <who, doing what>. It must never be mistaken for <anti-reference>.

## Interview
1. Who and when: <answer> (user | assumed)
2. Feels like: <answer> (user | assumed)
3. Never: <answer> (user | assumed)

## Direction
<bank name or "own: name">, audacity <safe | bold | wild>.
Runner-up: <name>, rejected because <reason>.
Taste words: <words> → <which word owns type and colour, which gets motion>.

## Knobs
- Type: <display family>, <text family>, <mono if any>; base <rem>; ratio <n>; weights <list>; why <reason>.
- Colour: <token>: <hex>: <role>, one line each; the contrast of each text/background pair.
- Density: unit <px>; row height <px>; section padding <px>.
- Shape: radius <values per element class>; border <spec>; shadow <spec or none>.
- Motion: durations <ms>; easing <curve>; under reduced motion <what remains>.

## Tokens
<once the token file exists: `sampo design extract <ui root>` writes this section, each custom property with its light
and dark value and the contrast of each text/surface pair; rerun it after a token change>

## Signature moves
1. <the one thing a screenshot is recognised by>

## Banned
- <anti-reference items, and any "Defaults to avoid" line that applies>

## Allowed exceptions
- <a default from the avoid list you keep on purpose, and why>
```

### Example, filled in

An invented product, to show the level of detail expected.

```markdown
# DESIGN.md: Kilnroom

## Point of view
It should feel like the studio's own glaze notebook, for adult evening potters booking wheel time on a phone
after work. It must never be mistaken for a gym-class booking app.

## Interview
1. Who and when: a hobby potter on the bus home, booking Thursday's wheel and checking a firing (assumed)
2. Feels like: a clay-dusted glaze notebook (user)
3. Never: a gym or fitness booking app (user)

## Direction
Field guide, audacity bold.
Runner-up: Toybox, rejected because its primaries read as a children's class.
Taste words: warm, calm → warm owns type and colour, calm gets motion.

## Knobs
- Type: a humanist serif for headings, a humanist sans for text, tabular figures for times; base 1rem; ratio
  1.333; weights 400 and 600; why: the serif carries the notebook, the sans keeps slot lists quick to scan.
- Colour: paper #F3ECE0: background; ink #2B2420: text (13.0:1 on paper); slip #C8B9A6: rules and borders;
  celadon #557463: available slot (white text 5.2:1); a booked slot is ink on paper with a slip hatch;
  iron #8C3B23: errors only, status colours are reserved (white text 7.6:1; see `type-and-colour.md`).
- Density: unit 8 px; slot row 48 px; section padding 48 px on phones.
- Shape: radius 6 on cards, 0 on rules and the calendar grid; border 1 px slip; no shadows.
- Motion: 200 ms ease-out on booking confirmation; under reduced motion the change is instant.

## Signature moves
1. Kiln slots drawn as shelves in a cross-section of the kiln, filling as people book.
2. Each firing's status shown as its temperature curve, with the current point marked.

## Banned
- Stopwatch or flame icons, progress rings, "crush your goals" voice: the gym anti-reference.
- Defaults to avoid 1, 3, 5 and 12.

## Allowed exceptions
- Default 9 (even spacing) inside the slot list only: equal rows make times comparable at a glance.
```

## Checks

Every line is pass or fail; run them on the built screens, not on the plan.

- [ ] `DESIGN.md` exists, names a direction, a runner-up with a reason, and an audacity setting.
- [ ] Each interview answer is quoted from the user or marked assumed.
- [ ] Every colour, size, radius and duration in the components comes from a token named in `DESIGN.md`
      (`grep -nE '#[0-9a-fA-F]{3,8}\b|[0-9]+px' ` over the component styles finds only token definitions;
      `sampo lib show design-systems` has the stylelint rule for colours).
- [ ] Each knob in `DESIGN.md` falls inside its direction's and taste word's row, or the deviation is listed
      with its reason.
- [ ] Each signature move is visible in at least one built screen.
- [ ] No line from "Defaults to avoid" appears unless listed under Allowed exceptions.
- [ ] The floors hold: contrast measured, focus visible, reduced motion tried, 200% zoom tried.
- [ ] The cover test passes on the main screen.

Example: `references/ui/examples/design-direction.html`: a DESIGN.md (interview answers, taste words, the chosen direction and its knobs) shown beside the screen it produced. The site's Gallery shows it.

## UI contract

- motion: the direction names its motion stance in DESIGN.md, the reduced-motion version included.
- contrast: DESIGN.md's palette is proven by the contrast script before any screen is built.
- keyboard: the direction includes its focus style, written in DESIGN.md with the rest of the look.
- small screens: the direction is sketched at 360 px first, then widened.
- states: DESIGN.md says how empty, loading, error and done look in this direction.
