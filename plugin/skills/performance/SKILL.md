---
name: performance
description: Use whenever you optimize or improve performance, or something is slow: profile, change one thing, measure.
---

# Sampo: performance

No tuning without a number. A PERF task's criterion states the metric and the target ("p95 under 200 ms"). No brief yet → `/sampo:intake` first.

1. **Baseline.** Run the workload several times and record the numbers, noise band included: `../playbooks/references/perf/benchmark-statistics.md`. Save the command in the brief with `sampo task evidence ID --step 1 --run "<benchmark>"`.
2. **Find where the time goes** before guessing: `../playbooks/references/perf/profiling.md` (the biggest frame first). The stack's own profiler and the special cases are listed in `../playbooks/references/perf/INDEX.md` (database queries, regex backtracking, mobile startup, frame budget, latency-critical paths).
3. **One change, one measurement.** `../playbooks/references/perf/benchmark-loop.md`: keep it if the gain is outside the noise band, revert it if not. Never bundle two changes.
4. **Know the code you touch:** `sampo graph blast PATH` for what depends on it, `sampo sym PATH:NAME` for its callers. A faster path that fails an existing test is a regression, not a win.
5. **Record the result** as evidence: the before and after numbers from the same command on the same machine, then `sampo check --affected --evidence ID`.
6. **Stop at the target.** Further gains are a new task: `sampo capture --source discovered`.
