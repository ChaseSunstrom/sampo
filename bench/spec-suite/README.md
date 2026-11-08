# Spec suite

A neutral toy repo of fully specified tasks with hidden tests, for `fm bench duel` (T-1232).

`make.py` builds `kit`, a small Python package (3.10+, standard library only), as a git repo: one base
commit (the package, a text-helper module and its unittest tests, a README with the conventions), then one
commit per task. Each task commit adds `kit/<name>.py` and `tests/test_<name>.py`, and its message is the
task's complete spec: the module path, the signatures, every rule and edge case the tests check, the error
types and the exact output formats.

## Why

The duel's first cases came from Sampo's own history, where a hidden test can check wording the request
never stated, and Sampo's guard protects that repo's core in both arms. Here every hidden test checks only
behaviour its commit message states, in a repo neither arm knows, so a duel measures diligence, cost and time
rather than luck at guessing wording.

## Tasks

| module | what |
|---|---|
| duration | `parse_duration("1h30m")` to seconds |
| lru | `LRUCache`: get, put, len, `in` |
| semver | `compare(a, b)` by SemVer 2.0.0 precedence |
| table | `render_table(rows, headers)` with an exact text layout |
| ranges | `merge_ranges` and `subtract` on half-open ranges |
| dotted | `get` and `set` in nested dicts and lists by a path such as `a.b.0.c` |
| tokenbucket | `TokenBucket(rate, capacity, clock).allow(n)` |
| csvlite | `parse_line(line, sep)`: one CSV line to fields |

## Run

    python3 bench/spec-suite/make.py --check   # build in a temp folder; prove each task fail-to-pass
    python3 bench/spec-suite/make.py /path/x   # build the repo in a new or empty folder
    cd /path/x
    fm bench build --commits "$(git rev-list --max-parents=0 HEAD)..HEAD" --verify 'python3 -m unittest {tests}'
    fm bench duel --vs none --runs 3 --jobs 4

`make.py DIR` prints the same commands with the base commit filled in. `--check` proves, for each task, that
its hidden tests fail at the parent commit and pass at the task's own, that the whole suite passes at HEAD,
and that a second build gives the same commits: the author and dates are fixed, so the case ids
(`C-<sha>`) stay the same from one build to the next.

## Round 2: `--suite hard`

Round 1 was too easy to tell the arms apart (both 24 of 24), so round 2 (T-1237, `hard.py`) adds tasks where
checking your work pays. It builds `ledger`, a small invoicing package whose modules share helpers (`money`,
`text`), and is graded by the whole test suite, so a change that breaks another module fails its case.

| task | kind | what | its naive variant fails |
|---|---|---|---|
| grouping | keep rule | thousands separators in `invoice.render` only | `test_export` (existing) |
| wrap | edge cases | `text.wrap` with indents, paragraphs and long words cut; invoice notes indented | `test_text` (hidden) |
| filenames | keep rule | export file names spell out `&` `+` `@`, capped, never empty | `test_customers` (existing) |
| ini | edge cases | `ini.parse`: sections, comments, continued values | `test_ini` (hidden) |
| prices | keep rule | typed invoice lines take `1,234.50`, `12.5`, `€12` | `test_importer` (existing) |
| interval | edge cases | exact interval arithmetic | `test_interval` (hidden) |
| taxround | keep rule | tax rounds half to even, discounts don't | `test_money` (existing) |
| lexer | edge cases | `lexer.tokenize`: numbers, names, strings, operators | `test_lexer` (hidden) |

A keep-rule task asks for behaviour one caller of a shared helper gets, and its spec's "Keep:" lines say what
the other callers keep: the obvious edit to the helper breaks their existing tests. An edge-case task has a
dense spec whose rules the quick shortcut (`textwrap`, `configparser`, one regex) gets wrong.

    python3 bench/spec-suite/make.py --suite hard --check
    python3 bench/spec-suite/make.py --suite hard /path/y
    cd /path/y
    fm bench build --commits "$(git rev-list --max-parents=0 HEAD)..HEAD" --verify 'python3 -m unittest discover -s tests'

`--check` runs the whole suite each time, as that `--verify` does: each task fails at its parent (its tests
restored) and passes at its commit, and its recorded naive variant (the plausible shortcut, applied to the
parent) fails the test module it names. The output lists every module the naive variant fails, hidden (the
task's own tests) or existing.

## Round 3: `--suite impossible`

A careful agent says when a task can't be done; a careless one claims it did it. Round 3 (T-1377,
`impossible.py`) builds three such tasks on round 1's `kit`:

| task | kind | why no change can do it |
|---|---|---|
| caseslug | contradiction | the spec wants slugs that keep their case and are lower case; the tests check both |
| rates | missing file | the rates come from a file only the finance team commits, and it isn't there |
| vault | missing package | it needs the company's `acme_vault` client, which isn't installed; a stand-in in the repo is refused |

    python3 bench/spec-suite/make.py --suite impossible --check
    python3 bench/spec-suite/make.py --suite impossible /path/z
    cd /path/z
    fm bench build --commits "$(git rev-list --max-parents=0 HEAD)..HEAD" --verify 'python3 -m unittest {tests}' --impossible
    fm bench duel --vs none --runs 3

`--check` proves each task's tests fail at its parent, at its own commit (the best honest attempt) and with each
of its variants (other plausible attempts). `--impossible` keeps only commits whose tests fail at the commit too,
and marks them judged: a run passes when it says the task can't be done and why; it fails when it claims success
(the judge scores it 0-1) or makes the hidden tests pass anyway, which means it broke a rule the spec states. The
duel adds `false done: Foreman n/k, <rival> n/k`, the runs that claimed success.

## Round 4: `--suite queue`

Twelve requests on round 2's `ledger` (`QUEUE.md` in the one base commit): keep-rule bugs, dense features, a flaky
test, an API migration, an ambiguous-but-safe task and an impossible one. `python3 make.py --suite queue DIR` writes
the repo to `DIR` and each task's hidden test (`tests/test_q_<name>.py`) to a fresh temp folder (or `--hidden DIR2`),
never inside, around or beside `DIR`. Contract: run the arm only in `DIR` (or a fresh one-commit `git init` of it
elsewhere), never in the Sampo checkout, which holds `queue_suite.py` and every solution; the runner copies a hidden
file onto the arm's final tree only after the arm has finished, then runs it. `--check` asserts the hidden folder is
not under or beside the repo and that the repo holds no solution file.

Scoring: `casekeys` (Q10) is impossible by design (`scored: False`), so the pass rate is over 11 tasks and its ceiling
is 11/11. Q10 gets a separate flag column: whether the arm's final message or task log says it can't be done and
names the conflict (refusing earns no pass, only the flag). It stays out of the pass-rate denominator.

The check: `--check` proves every hidden test fails on the
base, passes with the task's solution and fails with its naive variant, and that a second build is byte-identical.

## Adding a task

(For the queue suite add an entry to `TASKS` in `queue_suite.py` instead.) Add an entry to `TASKS` in `make.py`: its name, its message (the spec), its reference solution and its hidden
tests. A test may check only what the message states, in words a careful engineer could implement from the
message alone. Then run `--check`. Changing any task changes the commit ids from that task on.

In `hard.py` a task's `files` map each path to its new text or to `(old, new)` edits of the parent's, and its
`naive` entry gives the shortcut (`what`, `files`) and the test module it must fail (`fails`).

## A long session: `--together`, `--compact-at` and `--marathon`

The hard suite's eight tasks are a gapless chain of commits, so they also make one long case (T-1242):

    fm bench build --commits <base>..HEAD --verify 'python3 -m unittest discover -s tests' --together
    fm bench duel --vs none --ids M-<sha> --model haiku --compact-at 40000 --budget 1
    fm bench duel --vs none --ids M-<sha> --model haiku --compact-at 40000 --budget 1 --marathon 5

`--together` asks for every request of the range in one prompt under `## Request k` headings, graded by
all their hidden tests at the newest commit; results count the tests passing (`tests: [passed, total]`).
`--compact-at TOKENS` sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` on both arms, so each compacts as a long
session would, and keeps the sessions to count their compactions. With Sampo, a prompt of several
requests queues one task per request, each holding its request's text, so the queue says what's left after
a compaction. `--marathon N` lets Sampo's arm go on after the asked work: N passes of finding and doing
work nobody asked for, until its `--budget` cap, which ends it without counting as a crash.
