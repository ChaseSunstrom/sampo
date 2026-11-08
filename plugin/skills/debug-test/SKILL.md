---
name: debug-test
description: Use whenever you debug, a test fails, a run crashes or a bug comes back: reproduce, root cause, fix.
---

# Sampo: debug a failure

Root cause before any fix: the iron law of `../intake/references/debugging.md`. No brief yet? This is a FIX: `/sampo:intake` first (the hooks refuse edits without one).

1. **Read the failure, not the whole log.** Pipe the run into `sampo fail` (the test, the error, the repo frames, the failing source), a stack trace into `sampo trace`, a long log into `sampo logs` (`--since-good` shows what's new against a good run).
2. **Reproduce** with one command that fails the same way every time. It passes and fails with no code change → `../playbooks/references/fix/flaky-tests.md` first. Hard to pin down → one `sampo:fm-reproducer` (brief from `sampo lane brief ID`) commits the smallest failing test.
3. **Narrow it down**, cheapest first:
   - where: `sampo whyred --suspects` ranks files from the failing run's output;
   - when: it passed at an older commit → `sampo bisect --run "<cmd>" --good REF` names the first bad commit and its files;
   - which part of this task's change: `sampo whyred "<cmd>"` finds the smallest hunks that turn it red;
   - which values: `sampo record "<python cmd>"` shows the locals where the exception unwound;
   - where the bad value starts: trace it upstream with `../intake/references/debugging-root-cause-tracing.md`.
4. **One hypothesis at a time**, written down: `sampo task hypo ID add "<claim>"`, change one thing, rerun, then mark it ruled-out or confirmed. Keeps coming back → one `sampo:fm-debugger` with the failure, what was ruled out and the suspects.
5. **Fix test-first** (`../intake/references/regression-test.md`): red on the old code, green on the fix; `sampo task prove ID --run "<cmd>"` shows both. A sleep or timeout in the test → `../intake/references/debugging-condition-based-waiting.md`; guard every layer the bad value crossed → `../intake/references/debugging-defense-in-depth.md`.
6. **Prove it:** `sampo task evidence ID --step N --run "<cmd>"`, `sampo check --affected` while iterating, then `sampo check`. Something that got you past the obstacle goes in `sampo fix add` for the next session.

Three failed fixes on one step → stop: it's the design, not the line. Write the diagnosis and `sampo task block ID "<why>"`.
