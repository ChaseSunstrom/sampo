---
name: directions
description: Use whenever a new UI needs a look: six style directions, the pick saved as DESIGN.md.
---

# Sampo: style directions

One sentence about a product in; six distinct style directions out, each a card that fits on one screen; one is picked and saved as `DESIGN.md` with an audacity level. Start it with `/sampo:directions <sentence>`, or when the user asks for looks before a UI exists. It is for web and app screens; a terminal UI or a Claude Code mod takes the playbooks' `terminal-ui.md`.

The full procedure is in `references/procedure.md`: read it before any step, and read it again after a compaction. It widens the UI playbook `../playbooks/references/ui/design-direction.md` from one direction to six; the card format and the set's rules are in `references/cards.md`.

The looks: `sampo looks` ranks the twenty by the user's taste. Draw the looks you offer from its top, never one below 0 unless the user names it or none at 0 or above fits, then the least negative. Nothing detects a kept or reverted look on its own: run `sampo looks keep` or `sampo looks revert` yourself when the user does.

## Procedure

1. **A brief, and an existing direction.** Work inside a focused task. A repo's own direction (a `DESIGN.md` with a `## Direction` section, a brand guide, a design system) wins: name it and stop, unless the user asks for a new look.
2. **Read the sentence.** Take the product, audience and taste words; answer interview questions 1 and 3, marked *assumed*; never ask question 2.
3. **Draft six cards** in `references/cards.md`'s format and hold the set to its rules: at least four from the bank or the looks, at most two own; a spread of ground, type, density, motion and audacity; each text pair at 4.5:1; redraft a failing card at most twice.
4. **Show them.** All six, the comparison table, your recommendation and runner-up. Diverge when torn: `sampo design diverge 3`, `/sampo:looks tournament`, `sampo looks opposite LOOK`, `sampo looks book`, or the five idioms in `references/idioms.md`.
5. **The pick.** Standard autonomy: one AskUserQuestion, "Which direction?" then "How bold?". A look the user turns down goes to `sampo looks not`; a saved one the user replaces goes to `sampo looks revert`. Full autonomy: take the recommendation and record it with `sampo decide`, naming the runner-up. Record nothing in `sampo looks`: it holds the user's choices only.
6. **Save DESIGN.md** at the repo root with the playbook's template headings. The Direction line: `<bank row, look-NAME or "own: NAME">, audacity <safe | bold | wild>.`; a look that calls for sound takes `references/sonic.md`. An existing Tokens section stays; `sampo design extract` writes one when there is none. Check it as the procedure says.
7. **Hand on.** Build the playbook's step 7 and its Checks; preview pages stay out of the repo. A second run finds the saved DESIGN.md and stops at step 1 unless the user asks for a new look.
