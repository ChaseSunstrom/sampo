# Decomposition recipes (reference)

_Foreman's own reference (T-0624)._

Patterns for cutting a task into steps that can each be checked on their own. Pick the one that fits; `planning.md` covers step order and obligations.

- **One observable per step.** Each step ends in one thing that can be seen: a test passes, a command prints X, a page shows Y. A step whose done-state can't be observed is either part of the next step or two steps.
- **Walking skeleton** (FEATURE L, first step). Build the thinnest path through every layer, end to end, behind a failing acceptance test, then thicken it. Integration risk shows up on day one instead of at the end.
- **Spike, then build.** A `Spike:` step answers one question (does the API paginate, how slow is the query). It is time-boxed and thrown away, and its result is a note or a verified assumption, never shipped code.
- **Expand / migrate / contract.** For a schema, API or format change: add the new form beside the old one (expand), move callers and data over (migrate), then remove the old form (contract). Every step stays deployable, and each has its own rollback.
- **Test ladder.** Climb from the cheapest check that could fail to the most realistic: unit, then integration, then end to end, then live. Stop climbing when a rung proves the criterion.
- **Strangler.** To replace a component, route one path at a time to the new one, keep the old one serving the rest, and delete it when nothing routes to it.
- **Contain, cure, inoculate** (FIX). First stop the harm (a guard, a revert, a flag), then fix the cause, then add the test or guard that keeps it fixed.
- **Full plan** (a big task: tier XL, HUGE or GIANT, T-4221). It can't start until its brief holds Design (the interfaces every child relies on), Breakdown (one `[key] Title` block per child with `files:`, `after:`, `covers:`, `criterion:` lines) and Integration (criteria that hold once the children are in). `sampo big plan ID` writes the skeleton, `sampo big check ID` lists each problem with its fix (a criterion no child covers, a child above M, a cycle, two children of one wave owning a file, a verify that can't fail), `sampo big expand ID` makes the child tasks, idempotently (`--dry-run` first). A GIANT's Breakdown lists milestones (XL or HUGE, `outcome:`), each planned in full when it starts.
