---
name: scientific-debugging
title: Scientific debugging
area: practice
tags: [debugging, hypothesis, bisect, reproduction, root-cause]
detect: []
related: [rubber-ducking, five-whys, experiment-design, red-team-review]
---
Debug by reproducing first, then testing ranked hypotheses with cheap discriminating probes, changing one thing at a time.

## Verify
- `sampo task prove --help`: records the failing repro before a fix.
- `sampo task hypo --help`: records hypotheses and probes on the task.
- `git bisect log`: if you bisected, the log shows good and bad commits and the culprit.
- The repro command now exits 0 and the same command failed before the fix.

## Test first
The repro is the test. Write the smallest command that fails for the reported reason.
```bash
# repro: fails with "KeyError: 'id'" before the fix
python -m pytest tests/test_orders.py::test_missing_id -x -q
```
Add the expected failure text next to it. If you cannot make it fail, you have not reproduced it, so stop and gather data.

## Do
- Reproduce first; shrink the input until removing anything makes the failure vanish.
- List 3 hypotheses ranked by likelihood; write for each one probe that separates it from the others.
- Run the cheapest probe: print a value, assert an invariant, check a log, bisect a commit range.
- Change one variable per run; record the result of each, including negatives.
- Read the actual error, the full stack, and the input that triggered it before guessing.
- Fix the cause, then run the repro, the neighbouring tests and the full gate.
- Keep the repro as a regression test.
- After three failed attempts, write what is ruled out and hand the failure to a fresh reader (`sampo:fm-debugger`).

## Avoid
- Editing code to "see if it helps": write the hypothesis and the expected observation first.
- Fixing a symptom (a try/except, a default): trace to why the bad state arose.
- Several changes between runs: you cannot tell which one mattered; revert to one.
- Trusting a passing run you cannot reproduce: flaky means a cause exists, find it (ordering, time, shared state).
- Deleting the repro after the fix: keep it as a test.

## Review
- [ ] The failure reproduced before any fix.
- [ ] Hypotheses and probes were written, with results.
- [ ] The fix addresses the cause, not the symptom.
- [ ] The repro is now a permanent test.
- [ ] Full gates pass after the fix.
