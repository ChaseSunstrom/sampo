---
name: improve
description: Use whenever asked to improve Sampo itself, or its self-inbox has items and the queue's empty: eval-gated changes.
---

# Sampo: improve (bounded, eval-gated)

Sampo improves its own skills, rules, hooks and scripts — never the model — and never unreviewed.

1. **Collect.** The self-inbox: `sampo state -p <foreman project slug> --json` → inbox items with `source: self` (the slug is the project registered for ~/.claude/foreman; see state/registry.md). Add signals: `sampo doctor`, `sampo tidy`, hook latency (`sampo watch --once`), recurring task types (3+ similar tasks → propose a skill, built and evaluated with skill-creator).
2. **Propose ≤ 5 changes**, each with its evidence and expected effect. Show the proposal and ask one yes/no question; continue on yes. In full autonomy, build and evaluate without asking and bring the result to the final report.
3. **Isolate.** `git -C ~/.claude/foreman worktree add ~/.claude/foreman-improve-<date> -b improve/<date> main`. Build candidates only there. Never edit the live `~/.claude/foreman/plugin`.
4. **Protected core** — Ward (lib/fmguard.py, hooks/hook, hooks/hooks.json), deny rules and permission settings, the evidence and approval rules (rules/sampo.md), the eval suite (evals/**) and BUILD_PROMPT.md: these need the user's yes for that brief: `sampo ask ID core --why "…"` and one yes/no question (their reply grants it; you can't).
5. **Referee.** Run the eval suite against both, with a cost ceiling:
   `claude plugin eval ~/.claude/foreman/plugin --max-cost-usd 5 --json <tmp>/live.json`
   `claude plugin eval ~/.claude/foreman-improve-<date>/plugin --max-cost-usd 5 --json <tmp>/candidate.json`
   Then the real-work referee (T-0212): `sampo bench run --label live-<date>` and `sampo bench run --plugin ~/.claude/foreman-improve-<date>/plugin --label cand-<date>` replay finished Sampo tasks (cases from `sampo bench build`, each validated fail-to-pass) and `sampo bench compare live-<date> cand-<date>` shows pass rate, cost and turns side by side. Use the same cases, model and `--runs` (3 when the budget allows) for both, then `sampo bench gate live-<date> cand-<date>`: exit 0 only when the candidate loses no passes in total and costs at most 15% more per case. With `--runs` above 1 each delta shows a ± band (2 standard errors from the attempts): a loss inside it reads "within noise" and doesn't fail the gate, and a gain inside it is not a win to claim. A candidate that fails the gate is not proposed. Before proposing, also run `sampo bench court --plugin <candidate>` (your past steers and corrections as judged cases): a candidate that stops honouring one is not proposed either.
   Compare per-category scores and per-case results. A candidate qualifies only if it scores at least as well in every category, regresses no case (eval or bench), costs no more per bench case, stays within the §2.8 budgets (`python3 plugin/tests/bench_hooks.py`, rules ≤ 80 lines) and `sampo doctor` is green in the worktree.
5b. **Automated generations** (T-0224): for a change to one instruction file (a skill, reference, rules), `sampo evolve --target <path> [--drop] [--ids …] [--runs 3]` does steps 3–5 in one command — a tool-less child revises the file from bench failures and friction (or `--drop` empties it: does it earn its tokens?), the candidate is committed on `evolve/<time>` in a worktree beside the repo, both arms replay the same cases, and only a candidate that passes `sampo bench gate` is kept. Kept branches go into step 6's table.
6. **Ask for the merge** with a table: change → evidence → eval delta → budgets, and one yes/no question. Merging is always the user's call, whatever the autonomy setting (in full autonomy it waits for the final report).
7. After the user's yes: merge to main, bump `plugin/.claude-plugin/plugin.json` version (and marketplace.json), update MASTER.md and CHANGELOG.md in the same commit, `claude plugin tag plugin`. Rollback: `git revert` + `/reload-plugins`.
