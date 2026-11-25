---
name: playbooks
description: Use whenever you fix build errors, do TDD, plan, write an ADR or run evals.
---

# Sampo playbooks

On-demand procedures, ported with attribution from ECC (MIT; see `plugin/THIRD_PARTY_LICENSES.md`). Load only the one you need: `sampo playbooks show PATH` adds this project's facts and its `.claude/playbooks/` overlay; `sampo playbooks eval PATH` scores the shipped text before you edit it; `route STAGE`, `blame PATH`, `preview PATH`; `draft ID`, `mine`, `counter`; `score NAME --task ID`, `kata NAME`, `lesson RANGE`. They supplement the Sampo loop and never override it: the evidence rule, `sampo` state, the guard and the focus lock still apply. Where a playbook names an ECC agent or command: planning → `/sampo:intake`; review agents → `sampo:fm-reviewer`; security review → `/security-review` and `references/security/security-review.md`; TDD → `../intake/references/regression-test.md`; session save and resume → `sampo checkpoint` and `/sampo:resume`.

Core debugging, regression-test and verification procedures live with intake: `../intake/references/debugging.md`, `../intake/references/regression-test.md`, `../intake/references/verification.md`.

Each stage's index names its playbooks, with one line each saying when to load it.

## PLAN — expanding and planning (R1/R4)
`references/plan/INDEX.md`: acceptance criteria, planner, API design, decision memo.

## GROUND and RESEARCH — reading real code, reuse before build (R2)
`references/ground/INDEX.md`: architecture from patterns, execution tracing, search-first.

## CLEAN — behavior-preserving refactors
`references/clean/INDEX.md`: simplifier, repo sweep, characterisation tests, deprecation, dead code.

## PERFORMANCE — measure before and after
`references/perf/INDEX.md`: profiling, the benchmark loop, noise and statistics.

## SECURITY — review, hardening, triage
`references/security/INDEX.md`: the security checklist, command guard rules, cloud infrastructure, triage.

## BASELINE / FIX — build and compile errors
`references/build/INDEX.md`: build and compile errors per toolchain. `sampo playbooks show` the index, then the one you need.

## FIX / FEATURE / VERIFY — TDD and verification
`references/verify/INDEX.md`: TDD, the verification loop, per-stack proof. `references/fix/INDEX.md`: bugs by kind (flaky tests, SQL, migrations, text and time, CLI, data pipelines).

## REVIEW — checklists (also read by sampo:fm-reviewer)
`references/review/INDEX.md`: code, language, security, silent failures, test coverage, type design, surface parity.

## Language references — idioms and testing
For one framework, library, service or tool, use the stack library instead: `sampo lib` suggests this repo's cards, `sampo lib show NAME --section verify` reads one part (`../library/SKILL.md`). `references/lang/INDEX.md`: C++, Python and Rust patterns and testing, the Python standard library first.

## UI — design direction, motion, 3D, terminals and mods
`references/ui/INDEX.md`: direction, colour, composition, motion, 3D, visual QA, terminals and mods.

## OPS — incidents and observability
`references/ops/INDEX.md`: incident response; logs, metrics and traces worth having.

## RELEASE — versions and dependencies
`references/release/INDEX.md`: releasing a version, adding or upgrading a dependency, shipping a branch.

## REFLECT — decisions and retros
`references/reflect/INDEX.md`: agent introspection, architecture decision records.

## Context and memory hygiene
`references/context/INDEX.md`: context budget, living docs, strategic compaction, plain writing, decks and reports.

## Evals and self-improvement
`references/evals/INDEX.md`: eval-driven development, agent loop design checks.
