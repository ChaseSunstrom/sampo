---
description: Your taste in looks: the twenty looks ranked by what you kept, a 12-pair calibration, or learn from files you name
disable-model-invocation: true
argument-hint: "[calibrate | keep LOOK | revert LOOK | not LOOK [why] | learn PATH… | tournament | opposite LOOK | book | roll | bisect | seance | history [PATH]]"
---
Sampo's taste ledger for looks, through `sampo looks` (the Smith app and a terminal use the same verb).

Arguments (may be empty): $ARGUMENTS

1. No arguments: run `sampo looks` and show its ranking as it is (each look's score and what moved it).
2. `calibrate`: run `sampo looks calibrate --json`. It prints a `round` and twelve pairs, each two looks with a title and
   a one-line picture. Ask them in three AskUserQuestion calls of four questions each, in order. Each question:
   header `Pair N`, question "Which is more like you?", options A (`title: line` of the first look), B (the second),
   and "Neither". Then record all twelve at once: `sampo looks calibrate --round R --answers a,b,n,…` (a for A, b for B,
   n for Neither or an answer that fits neither, in pair order). Stopped before twelve, or AskUserQuestion is denied:
   record nothing, and say so.
3. `keep`, `revert` or `not` and a look: run `sampo looks keep LOOK`, `sampo looks revert LOOK` or
   `sampo looks not LOOK --why '<their words>'`; the look is its card name (`look-lido`), short name or title. The
   words go in single quotes, so they must hold no quote character (' " `) and no line break: if they do, refuse in
   one line ("say the reason without quotes") and run nothing.
4. `learn` and paths: run `sampo looks learn 'PATH'…` with exactly the paths the user typed, each in single quotes,
   nothing added; a path holding a quote character is refused the same way. It reads only those local files (a page,
   a stylesheet, a photo when Pillow is installed), read-only and capped, never a URL, a folder or a symlink, and
   keeps only the file's name and the look it matched. Never pass a path the user didn't type.

5. `tournament [LOOK…]`: run `sampo looks tournament --json` (or with the looks the user named: 2, 4 or 8). It prints
   `next`, the matches still to answer, and `then`, the command to continue. Ask each match with AskUserQuestion
   (options A and B, `title: line` of each look; no Neither), run `then` with their answers appended, and repeat
   until it names a champion. Denied or stopped early: record nothing, and say so.
6. `opposite LOOK`: run `sampo looks opposite LOOK` and show the three looks farthest from it (opposite day).
7. `book [LOOK…]`: run `sampo looks book --out lookbook.html` (or with the looks named) and say where the page is.
8. `roll`: run `sampo looks roll` (the look of the day, leaning on the ranking). `bisect`: `sampo looks bisect` (where the
   taste turned). `seance`: `sampo looks seance` (the looks turned away from, their last words). Show what sampo printed.
9. `history [PATH]`: run `sampo looks history` (DESIGN.md, Sampo's own record of the look) or, only when the user typed
   a path, `sampo looks history 'PATH'` (quoted as in 4). It reads that file's git history and records each look it named
   as kept (a week or more) or reverted (replaced sooner), once per stint.

Kept and reverted are detected only when `sampo looks history` runs. Whenever the user keeps a look (picks it, ships it,
says it's the one) or reverts or replaces one, in this command or anywhere else,
run `sampo looks keep` or `sampo looks revert` yourself; otherwise the ledger hears of it only when its history is next read.

Reply in at most 8 lines: what sampo printed, then that `/sampo:directions` drafts from the top of this ranking.
