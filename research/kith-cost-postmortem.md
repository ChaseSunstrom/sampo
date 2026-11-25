# Kith cost post-mortem (T-6200)

Kith's one lead session ran from 2026-11-12 to 2026-11-22. This report covers that session's transcript and its 1,078 subagent transcripts (2.4 GB). Prices are list prices per model family (`plugin/data/model-prices.json`, dated 2026-11-22).

The numbers come from Sampo's own readers: `sampo cost -p kith-f1978b --days 14` with `--sessions`, `--outputs`, `--repeat-reads` and `--by-model`. One more script covered what they don't split: turn cost by trigger, context rent by source, subagents by type, and cost by day. It lives in `~/.cache/claude-ops/kith-pm/gaps.py`. The numbers below come from its second run, `gaps2.json`, which labels shell commands after any leading `cd` or variable assignment. The first run, `gaps.json`, is kept beside it for comparison.

Both ways of counting agree on cache reads: 9.70 B tokens each, deduplicated by message id.

## Totals

| | Dollars | Share |
|---|---:|---:|
| Lead (Opus) | 3,157 | 48.5% |
| Subagents | 3,350 | 51.5% |
| **All** | **6,507** | 100% |

- **Tokens by kind.** Cache reads were 9.70 B: 4.84 B on the lead, 4.86 B on subagents. Cache writes were 280 M, output 68.4 M and uncached input 0.2 M. Cache reads alone were about 63% of the dollars.
- **The lead.** It took 26,522 turns with an average context of 184k tokens per turn. Each turn cost about $0.12. It compacted 176 times and busted the cache 186 times: 179 busts came right after a context edit and 7 from an expired cache (`--sessions`, bust causes).

## Drivers, by dollars

Rows 1, 2, 4, 5 and 6 are separate dollars, ranked by size. Row 3 is the context every lead turn re-reads: its $2,421 overlaps every lead row, so it is a multiplier and is not ranked. Rows 7 and 8 are parts of rows 2 and 3.

| # | Driver | Evidence | $ | Share | Task |
|---|---|---|---:|---:|---|
| 1 | Subagent model mix | fm-builder $1,761 (27.1%): 32.8k Sonnet, 8.5k Opus and 9.9k Haiku replies. general-purpose $1,151 (17.7%): 6.7k Opus replies in 49 agents. The lead's launches of them named Sonnet 29 times, nothing 6 times and Opus 4 times; 10 have no Agent launch in the lead (not traced; a Workflow agent inherits the lead's model). fm-reviewer $389 (6.0%) | 3,301 | 50.7% | Mostly fixed since: T-6101 (Haiku default, haiku mode), T-6164 (reviewers on Haiku), T-5497 (fit flag). Still open: an untyped or general-purpose Agent or Workflow launch inherits Opus, T-6308 |
| 2 | The lead investigates by hand | 10,385 lead turns started by grep, sed, Read, cat, tail, ls, for and git output | 1,227 | 18.9% | T-6309: a streak of read-only calls goes to a Haiku scout |
| 3 | The lead's context size | 184k per turn, $2,421 of lead cache reads (37% of all dollars). Lead re-reads, as shares of all cache reads: thinking 7.9%, skill listing 2.0%, compact summaries 1.8%, invoked skills 1.2%, queued commands 1.1%. Hook text kept in context: SessionStart $73, PostToolUse $49 of rent | 2,421 (overlaps) | multiplier | T-6310: a budget on the fixed per-call parts, hook text included, average under 120k |
| 4 | Wake-ups | Stop-hook blocks: 946 turns, $108. Task notifications: 1,462 turns, $177. Compactions: 166 turns, $55 | 341 | 5.2% | T-6311: no Stop block when only waits remain. Fewer notifications come from headless lanes with one digest (T-5960) |
| 5 | Sampo bookkeeping by hand | 2,940 sampo calls: task show 766, task set 623, task evidence 391, focus 323, task new 291, task log 283, task ac 263 | 337 | 5.2% | T-6312: fold the top sequences into single verbs |
| 6 | Workaround scripts | 1,433 inline `python3 -` scripts, each one a missing verb | 192 | 3.0% | T-6312, plus T-5731's turn-waste ledger, which counts them from now on |
| 7 | Tool output kept in context | 19.8% of cache reads. Subagents: Read 5.4%, sed 1.8%, grep 1.6%. Lead: sed 1.6%, grep 1.2%, Read 1.0% | (in 2, 3) | | T-6309 and T-6310. Briefs that carry the code a builder needs (T-5498) |
| 8 | Repeated reads | 55 repeats out of 17,128 reads, 111 KB (about 28k tokens) | ~0 | | none needed |

The lead's turn triggers in rows 2, 4, 5 and 6 cover $2,098 of its $3,157. The remaining $1,060 is spread over smaller triggers, none above $74: Agent $73, SendMessage $60, Edit $57, cargo $41, Write $40, ps $28, and many under $28.

## Cost by day

Lead and subagents, in dollars:

| Day | Lead | Subagents | Total |
|---|---:|---:|---:|
| 11-12 | 115 | 25 | 140 |
| 11-13 | 293 | 66 | 359 |
| 11-14 | 577 | 122 | 699 |
| 11-15 | 486 | 432 | 917 |
| 11-16 | 283 | 193 | 476 |
| 11-17 | 186 | 46 | 232 |
| 11-18 | 543 | 408 | 951 |
| 11-19 | 172 | 620 | 792 |
| 11-20 | 269 | 882 | 1,151 |
| 11-21 | 80 | 299 | 379 |
| 11-22 | 154 | 257 | 411 |

Subagent spend peaked on 11-19 and 11-20. Over the whole run, 81% of fm-builder replies came from Sonnet or Opus. Haiku routing (T-6101) landed on 11-22, so this transcript holds no full day after it.

## What this means for Sampo, in any repo

- **Model mix is half the bill.** The largest single lever has been the routing rule since 11-22 (T-6101): Haiku by default, Sonnet only after two failed Haiku rounds, Opus only for art and UI. This transcript ends that day, so it cannot show the rule holding; the next post-mortem should check it. The leak still open is untyped launches, covered by T-6308.
- **Every lead turn costs its whole context.** Cutting turns (T-6309, T-6311, T-6312) and cutting context (T-6310) multiply together.
- **Kith stays stopped** until T-6200 and T-6308 to T-6312 land. `~/.cache/claude-ops/scripts/resume_at_reset.sh` says so, and nothing restarts kith automatically.
