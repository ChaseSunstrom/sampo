---
name: playbooks
description: Use whenever you fix build errors, do TDD, plan, write an ADR or run evals.
---

# Sampo playbooks

On-demand procedures, ported with attribution from ECC (MIT; see `plugin/THIRD_PARTY_LICENSES.md`). Load only the one you need: `sampo playbooks show PATH` adds this project's facts and its `.claude/playbooks/` overlay; before editing one, `sampo playbooks eval PATH` scores the shipped text against its held-out briefs (`plugin/evals/playbooks/`), with and without it; `route STAGE`, `blame PATH` (lines to tasks), `preview PATH` (checks it adds); from this repo's work, `draft ID`, `mine` (files changed together), `counter` (what failed here); to learn, `score NAME --task ID`, `kata NAME [--answer FILE]`, `lesson RANGE` (a diff in five minutes). They supplement the Sampo loop and never override it: Sampo's evidence rule, `sampo` state, the guard and the focus lock still apply. Where a playbook mentions an ECC agent, command, hook or script, use the Sampo equivalent: planning → `/sampo:intake`; review agents → `sampo:fm-reviewer` or the checklist itself; security review → `/security-review` plus `references/security/security-review.md`; TDD → `../intake/references/regression-test.md`; session save/resume → `sampo checkpoint` / `/sampo:resume`.

Core debugging, regression-test and verification procedures live with intake: `../intake/references/debugging.md`, `../intake/references/regression-test.md`, `../intake/references/verification.md`.

## PLAN — expanding and planning (R1/R4)
- `references/plan/intent-driven-development.md` — Turn ambiguous or high-impact product and engineering changes into scoped, verifiable acceptance…
- `references/plan/planner.md` — Plan a complex feature or refactor: phases, risks, steps.
- `references/plan/api-design.md` — Adding or changing a public interface (CLI, endpoint, library, config or file format): caller-first…
- `references/plan/decision-memo.md` — A costly or contested choice: question, three options scored alike, choice, why, revisit trigger.

## GROUND and RESEARCH — reading real code, reuse before build (R2)
- `references/ground/code-architect.md` — Designs feature architectures by analyzing existing codebase patterns and conventions, then providing…
- `references/ground/code-explorer.md` — Deeply analyzes existing codebase features by tracing execution paths, mapping architecture layers, and…
- `references/ground/search-first.md` — Research-before-coding workflow. Search for existing tools, libraries, and patterns before writing…

## CLEAN — behavior-preserving refactors
- `references/clean/code-simplifier.md` — Simplifies and refines code for clarity, consistency, and maintainability while preserving behavior.…
- `references/clean/repo-sweep.md` — Whole-repo cleanup (repo, docs, settings, UI, briefs, dead and replaced code): inventory every area, a method per area, coverage as the done check.
- `references/clean/characterisation-tests.md` — Pin what untested code does today, before refactoring it.
- `references/clean/deprecation.md` — Retire a public surface: replacement first, shim, warn, announce, wait, remove.
- `references/clean/refactor-cleaner.md` — Dead code cleanup and consolidation specialist. Runs analysis tools (knip, depcheck, ts-prune) to…

## PERFORMANCE — measure before and after
- `references/perf/INDEX.md` — profiling, the benchmark loop, is the difference real (noise and statistics), latency-critical systems, regex backtracking.

## SECURITY — review, hardening, triage
- `references/security/INDEX.md` — the security review checklist, command guard rules, cloud infrastructure, bounty triage.

## BASELINE / FIX — build and compile errors
- `references/build/INDEX.md` — build and compile errors per toolchain: C++, Rust, TypeScript, Go, JVM, Swift, C#, shell. `sampo playbooks show` the index, then the one you need.

## FIX / FEATURE / VERIFY — TDD and verification
- `references/verify/tdd-guide.md` — Test-Driven Development specialist enforcing write-tests-first methodology. Ensures 80%+ test coverage.
- `references/verify/tdd-workflow.md` — Test-first for a feature, fix or refactor: red, green, refactor.
- `references/verify/verification-loop.md` — Before calling work done: build, types, lint, tests, secrets, diff, report.
- `references/verify/INDEX.md` — more: verifying per stack (plugins, data, games, Go, mobile).
- `references/fix/INDEX.md` — bugs by kind: flaky tests, SQL, migrations, text and time, CLI errors, data pipelines, mobile crashes, serial and bus links.

## REVIEW — checklists (also read by sampo:fm-reviewer)
- `references/review/code-reviewer.md` — Expert code review specialist. Proactively reviews code for quality, security, and maintainability. Use…
- `references/review/cpp.md` — Expert C++ code reviewer specializing in memory safety, modern C++ idioms, concurrency, and performance.…
- `references/review/python.md` — Expert Python code reviewer specializing in PEP 8 compliance, Pythonic idioms, type hints, security, and…
- `references/review/rust.md` — Expert Rust code reviewer specializing in ownership, lifetimes, error handling, unsafe usage, and…
- `references/review/security.md` — Security vulnerability detection and remediation specialist. Flags secrets, SSRF, injection, unsafe…
- `references/review/silent-failures.md` — Review code for silent failures, swallowed errors, bad fallbacks, and missing error propagation.
- `references/review/test-coverage.md` — Review pull request test coverage quality and completeness, with emphasis on behavioral coverage and…
- `references/review/type-design.md` — Analyze type design for encapsulation, invariant expression, usefulness, and enforcement.
- `references/review/typescript.md` — Expert TypeScript/JavaScript code reviewer specializing in type safety, async correctness, Node/web…
- `references/review/surface-parity.md` — A feature must reach every surface (CLI, plugin, app, pane): one source, a parity test from the code.

## Language references — idioms and testing
For one framework, library, service or tool, use the stack library instead: `sampo lib` suggests this repo's cards, `sampo lib show NAME --section verify` reads one part (`../library/SKILL.md`).
- `references/lang/cpp-coding-standards.md` — C++ coding standards based on the C++ Core Guidelines (isocpp.github.io). Use when writing, reviewing…
- `references/lang/cpp-testing.md` — Use only when writing/updating/fixing C++ tests, configuring GoogleTest/CTest, diagnosing failing or…
- `references/lang/python-patterns.md` — Pythonic idioms, PEP 8 standards, type hints, and best practices for building robust, efficient, and…
- `references/lang/python-testing.md` — Python testing strategies using pytest, TDD methodology, fixtures, mocking, parametrization, and…
- `references/lang/rust-patterns.md` — Idiomatic Rust patterns, ownership, error handling, traits, concurrency, and best practices for building…
- `references/lang/rust-testing.md` — Rust testing patterns including unit tests, integration tests, async testing, property-based testing…
- `references/lang/INDEX.md` — more: the Python standard library first.

## UI — design direction, motion, 3D, terminals and mods
- `references/ui/INDEX.md` — the UI playbooks: design direction, type and colour, composition, interaction, motion, game feel, 3D and its template, visual QA, terminals and mods. `sampo playbooks show` the index, then the one you need.

## OPS — incidents and observability
- `references/ops/INDEX.md` — incident response; logs, metrics and traces worth having.

## RELEASE — versions and dependencies
- `references/release/release-and-changelog.md` — Cutting a version: gate the tagged commit, bump everywhere, user-facing changelog, migration notes…
- `references/release/dependency-admission.md` — Before adding a dependency: avoid it, name, licence, health, audit, weight, pin.
- `references/release/dependency-upgrade.md` — Bumping a library or toolchain: one per task, read the notes, baseline, fix forward, verify, check the…
- `references/release/INDEX.md` — more: shipping a branch to main, demo production.

## REFLECT — decisions and retros
- `references/reflect/agent-introspection.md` — Structured self-debugging workflow for AI agent failures using capture, diagnosis, contained recovery…
- `references/reflect/architecture-decision-records.md` — Record an architectural decision as an ADR: context, options, choice, consequences.

## Context and memory hygiene
- `references/context/context-budget.md` — Audit what fills the context window (agents, skills, MCP, rules) and cut it.
- `references/context/living-docs.md` — Keep a long-lived project's documentation from rotting by assigning existing project docs clear…
- `references/context/strategic-compact.md` — Suggests manual context compaction at logical intervals to preserve context through task phases rather…
- `references/context/INDEX.md` — more: task-first docs; decks and reports.

## Evals and self-improvement
- `references/evals/eval-harness.md` — Formal evaluation framework for Claude Code sessions implementing eval-driven development (EDD)…
- `references/evals/loop-design-check.md` — Design a goal-oriented agent loop, and review it for the ways loops go wrong — spinning and burning…
