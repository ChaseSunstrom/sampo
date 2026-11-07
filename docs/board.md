# The Board

The live page's default view (`sampo watch --web`, T-3659): nine panels, each a small live instrument for one Sampo
mechanism, on one dark grid. `#view=net` keeps the older network view; the forge stays a setting.

| # | Panel | What it draws | Where the facts come from |
|---|---|---|---|
| 01 | LANES | a cluster per builder lane: its leased files as nodes, hot ones (written in the last 10 min) lit, the task as a pill | `board.lanes`: briefs with a lane, `lanes._stop`, unexpired leases (`bus._leases`) |
| 02 | MERGE TRAIN | a Sankey: lanes waiting → the gate → foreman/build, packets along the bands | `board.train`: the last day's `lane_builder`, `lane_merge`, `lane_train_red` events |
| 03 | CPU | stacked bars per minute (30 min) by kind, the dashed goal line, the cores busy now | `board.cpu`: a /proc ring read at most every 10 s, other = the machine's busy rest |
| 04 | THE LOOP | the words asked flowing into the brief: criteria (met or not), steps, the choice's confidence, the stage track | `board.loop`: the brief in focus |
| 05 | LIBRARY | a dotted globe (≤ 1500 dots, one colour per grid) over a kept-per-hour histogram | `stats` (cards, moons) and `board.library` (calling cards, `cards_grow`/`moons_grow` kept) |
| 06 | MODELS | agents → Claude, Jeff and Ollama models, each line as thick as its calls; GPU util and VRAM | `models`, `flows`, `jobs`, `gpu` |
| 07 | SAMPO | a terminal typing the real sampo commands run here, the queue as a table with status pills, the depends_on graph | `board.commands` (the hook's tool events), `queue`, `board.deps` |
| 08 | STATE | the state folder's newest entries as a tree, the newest lit, the latest decision as a card | `board.state` |
| 09 | TESTS | a cell per test module: cached, running, failing, idle | `board.tests`: test files, the run cache, the latest `check_run`, live processes |

## The kit

Three files under `plugin/web/live/`, no libraries, shared by every panel (and meant for the site demos and SVG card
skills to reuse):

- `board-tokens.json`: the palette, dark and light. `board.css` declares the same values as `--bd-*` custom properties
  (test_board keeps them equal); the canvases read them from there, so chrome and drawing never disagree.
- `board.css`: the grid (1 column under 700 px, 2 under 1200 px, 3 above), the panel chrome: a numbered accent chip,
  the UPPERCASE name and a dim "what", the accent label on the right, the canvas, a footer caption with an optional
  progress bar and a stat. 1 px rules, cyan L-brackets where they cross; no rounded cards, no shadows.
- `board.js`: `SampoBoard.boardModel(snapshot, history)` maps any snapshot (empty, partial or huge) to nine bounded
  panels; it is plain data and runs under node (test_board). `SampoBoard.mount(el)` builds the panels and returns
  `{update(model), still(on), show(on)}`. A panel's drawer is `{base(g, d, w, h, T, mem), live(g, d, w, h, t, T, mem)}`:
  `base` paints the still layer into a cached offscreen canvas (redrawn only when the data, size or palette change),
  `live` paints what moves each frame on top.

## Cost

One `requestAnimationFrame` loop for all panels. It stops when the tab is hidden, when the Board isn't the view shown,
in Still mode and under reduced motion (each then gets one frame drawn at a fixed instant); an IntersectionObserver
skips panels off screen; canvases are capped at 2× device pixels; particles are capped per panel. On the server, the
`board` field is one part per panel: each soft-fails to `null`, the slow ones are cached (3–30 s), and every string
passes `redact`, the home-folder and private-term redaction and a width cap before it leaves the machine.
