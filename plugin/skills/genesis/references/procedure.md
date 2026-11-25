# Rune: Genesis, the full procedure

A tower of generations toward one goal the user wrote (gen-0). Tower files change only through `sampo rune put`,
every choice goes through `sampo decide`, and anything that leaves the machine needs its grant.
Under full autonomy with a standing goal or mandate the user's own words back (a user task or steer), a drained queue with 10+ done tasks starts the tower by itself, once
(T-2178: gen-0 drafted from the user's own goal text, sealed, one `sampo decide` row); with no goal, an ended goal, standard
autonomy or a sensitive project, `sampo scout` only offers it and waits on the user's yes.
A user steer mid-run becomes a standing rule plus its check in the same task:
`sampo decide "<rule>" --cites "user steer <date>" --check "python3 plugin/tests/run.py test_…"` (or
`sampo rune check`, `sampo louhi scorecard`); the sweep stays red while a steer rule has no check or its check fails. Its lean substance row judges each Mission's net code and test lines by its gain: a
survival gain passes, an unmeasured one waits as pending, a measured lack of gain is red and files a subtraction task. Its proof-before-presentation row reads README.md, site/index.html and site/pages.py:
every result (N of M, a percentage, a ratio, a comparison) names its proof (an exam run, a task with evidence, live
data; `<!-- source: T-NNNN -->` cites one invisibly), a rule or UI sample says so with `<!-- rule -->`, and no line
says a capability passes before the scorecard does. From gen 2, step asks first for `gen-N/prediction.md` (the user's three
likeliest next steers, `N. Steer: … | Why: …`), sealed by its first put; every steer recorded with `sampo decide --cites
"user steer …"` is scored against it (a miss files an anticipation gap), and before the next seed
`gen-N/blind-spots.md` lists each class found from inputs Sampo didn't author (session replays, steer classes,
unfiled failures, inversion, one far-domain comparison) with Source, Rule and Check lines, the rule recorded with
`sampo decide --check`.

## Start (no gen-0 yet)

1. The user's own words are gen-0. Given only a line, or nothing: `sampo rune draft "<line>"` prints a whole gen-0.
   Show it; their MUST/NEVER lines stay verbatim, and anything it marks unconfirmed is kept or cut by them.
   It carries the user's standing intent (`plugin/templates/standing-intent.md`): under gen-0, over every later
   steer, read by every cold read and seed, and #13's exam fails a draft missing a clause.
2. `sampo rune init --file -` saves it read-only and sealed by its hash (refused if it holds a secret); drift fails
   if gen-0 ever changes, and the sweep turns red on a grant kind no earlier generation used.
3. `sampo rune spec` maps its §2 capabilities to an exam, a third held out for grading only.
4. `sampo rune exam` grades Genesis itself (#13) on the held-out third: drafts from a line, nothing and "super
   improve this" on generated projects; `--arm plain` is the baseline. `sampo louhi run --agent genesis` runs a whole
   generation in a throwaway repo. `sampo louhi long grade RUN --agent A` splits false dones: those whose
   failed checks all name something the goal never states, and real ones (T-2613).

## Drive

Repeat until the kind is done or stopped (on blocked, work the rest of the queue and come back):

1. `sampo rune step --json` names the one next action: {stage, gen, action, command, kind}.
2. Do it. Stages in order: spec (when spec.md is missing), seed (≤ 8 Missions), mission (`sampo mission`,
   `sampo ideas`), intake, build (each brief closed with `sampo task finish`), critique, select (`sampo rune select N`), drift (`sampo rune drift N`), review
   (`sampo rune review N`: a cold reader on a stronger model sees only the diff, exam, outcomes and friction; each
   finding becomes a task, `--handle K=gap` or `--handle "K=dismissed: why"`), gap map (redraw
   `gen-N/gap-map.md` against the current code, one recon per capability: a row each with tier 1-4, evidence and what
   is missing, plus `Drawn at: <HEAD sha>`; the next seed ranks Missions by expected value from it, the scorecard,
   survival and the review, empty cells only breaking ties), sweep (`sampo rune sweep N`), then the next seed (`sampo rune seed N+1`) or the report. The report step is blocked while a lineage choice is still provisional: after the
   production-model exam, run `sampo rune confirm N` for each (T-2197).
   From gen 2 the seed's 8th Mission is the moonshot (mission-moonshot.md): run `sampo rune frontier N`
   first, give each `### idea` its `Prior art:`, `Better by:` and `Novelty: 1-5` lines, run `sampo rune
   novelty N` (the cold read that marks obvious ideas), then name the `Spiked pick:` and its `Spike: shipped|failed`
   line; a failed spike goes to not-yet.md, and report.md lists each shipped moonshot with `(asked: …)`.
   When ideas, failures or misses keep fitting no category, `sampo rune grow KIND --name N --evidence E
   --measure M` adds one to frame.md (from gen 2; never removed). Give an added capability its coverage.md row
   before that generation's sweep, and list every added category in report.md (its F-N id and name). Today an
   added capability joins the exam spec, scorecard, seed and export; the other kinds are recorded and checked.
   Each seed carries "## The user's standing rules (steers)": every steer still in force (`c.steer_rules`, minus
   one a later steer `--reverses` or `--revisited`). Drift fails a seed missing one and only notes a steer newer than the seed.
3. After a tower edit, `sampo rune check` (exit 1 names every bad cell and not-yet gap; empty cells go to the next Seed, and any tower file hand-edited since its cold read: `sampo rune put` it again).

Kinds:
- blocked: a brief waits on the user or a grant; work the rest of the queue.
- fatal: the tower can't go on as filed; fix what the action names.
- stopped: three reverted generations in a row. Read lineage.md and the critiques, then write the next seed by hand
  or file the report. Only step stops: `sampo rune seed` still runs, so the next seed is a choice, not a default.
- done: report.md is filed.

## See it

`sampo rune status` prints the tower; `sampo rune live on` (a publish grant) keeps the site's genesis page current.
