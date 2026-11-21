---
name: coverage
description: Use whenever you plan a task or are asked for "all", "every" or "a ton": map the whole space first.
---

# Sampo: cover the whole space

Loaded at the plan of an M or L task, and on a request for everything. The first three items that come to mind are the likeliest and the least useful. Map the space, then choose from it; and cover what the user did not say, because the unasked need is where a finished task most often fails them.

## The core

1. **Write the space before the first item.** One line naming what is being covered (every caller of this function, every state a screen can be in, every verb the docs show). Then list its axes: the things, the actors who use them, the states each can be in, the ways each can fail. A grid of thing × actor × failure has cells nobody named; the empty cells are the findings.
2. **Enumerate from the source, not from memory.** Use the repo's own list: `sampo sym PATH:NAME` for callers, `sampo graph blast PATH` for what a change touches, `sampo map` for the layout, a grep for each spelling. For an open request, `sampo mission --request "<the words>"` and `sampo ideas --pack FILE --lens grid` cross the surfaces with the actors and failure modes. A list you wrote from memory is a first draft.
3. **Name the unasked needs.** For each item ask what the user would be angry to find missing, without having said it:
   - it keeps working for what already worked (callers, saved data, old files, an alias);
   - the empty, the huge, the repeated and the concurrent case;
   - the failure path: what the person sees when it goes wrong, and how they undo it;
   - docs, tests and the migration that the change makes stale;
   - who else reads this (another tool, a script, the next session).
   Write the ones that matter into the plan as steps or criteria. The ones that do not matter, drop with a word of why.
4. **Reason in rounds until the grounded yield runs dry.** Each round must see everything so far and add only what is new, and only what you can ground: a file you read, a command you ran, a quote from the request. A round that adds fewer than three grounded items is dry; stop there. An idea you cannot ground is not covered, it is guessed: mark it unverified or leave it out.
5. **Show the whole map, then cut.** Keep every cluster visible with its mark (build, defer, skip and why); do not hand over a hand-picked shortlist. A cut the user cannot see is a gap.
6. **Every item gets a check.** A listed case with no criterion or test is a wish. Put each on a step's evidence, or say it is out of scope in the brief's non-goals.

## When to stop and when to go wide

- A small, concrete ask ("rename this flag") needs steps 1 and 3 only, in a minute: the callers, and what breaks if one is missed.
- "All", "every", "a ton of", "fully featured": go wide, in rounds. Capture what you will not build now (`sampo capture "<idea>"`) so it is kept, not lost.
- Stop at the user's stated end (a number, a date, a named list). With none, stop when a round is dry, not when you are tired.

## Habits

- Search for the opposite: what is the one case that would make the list wrong?
- Count at the end: items listed, items checked, items cut with a reason. The three must add up to the space.
- Say in the report what you did not cover and why, in one line each.
