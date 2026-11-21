---
name: problem-solving
description: Use whenever a fix failed twice or you ask why something happens: rank causes, probe the cheapest first.
---

# Sampo: solve the problem, not the symptom

Loaded when a step's second run failed, or a "why" needs a real answer. Two failures mean the model of the problem is wrong, so another edit is the wrong next move. Think first, cheaply, in this order.

## The core

1. **Read it two ways.** Say the problem as the error states it, then as the user needs it ("the test fails" / "the export must keep its columns"). A fix that serves one reading and breaks the other is the usual third failure. Two readings that disagree: `sampo oracle` against the criterion, then pick one in writing.
2. **Decompose.** Split into the smallest parts that can each be true or false: the input, each stage it crosses, the output, the environment. Mark which parts you have *seen* work (a run, a log line) and which you only *believe*. The cause is in the believed ones.
3. **Rank hypotheses, each with one cheap probe.** Write at least three, most likely first, each with the single command that would show it false: `sampo task hypo ID add "<claim>" --probe "<cmd>"`. Include one you find unlikely and one about the setup (a stale build, the wrong directory, a changed dependency). Cheap before likely: a ten-second probe on a 20% guess beats an hour on a 60% one.
4. **Run the probes, one at a time, and record each:** `sampo task hypo ID mark N ruled-out --run "<cmd>"` (or `confirmed`). Never change code and probe in the same run; then nothing says which did it. Same error after two probes: your frame is wrong, return to 1.
5. **Bisect when the space is ordered.**
   - in time: it passed before. `sampo bisect --run "<cmd>" --good REF` names the first bad commit.
   - in the change: `sampo whyred "<cmd>"` finds the smallest hunks that turn it red.
   - in the data: halve the input, the config or the pipeline until one half still fails.
   - in the code path: print at the midpoint, not at the end.
6. **Invert.** Ask what would have to be true for this to be impossible, and check that: "this can never be called with an empty list": call it with one. Ask also how you would make it worse on purpose; the lever that does is on the cause's path.
7. **Fix the cause, test first.** Red on the old code, green on the fix (`sampo task prove ID --run "<cmd>"`). Then ask where else the same cause sits (`sampo graph blast PATH`) and whether a guard at the boundary makes it impossible, not only fixed.

## Stop rules

- A hypothesis confirmed by a probe that could have failed: fix it. A hypothesis only "consistent" with what you saw is still a guess.
- Four failed runs on one step, or no hypothesis left standing: write the diagnosis, ruled-out list and what you would try next, then `sampo task block ID "<why>"`. A blocked task with a diagnosis is progress; a fifth edit is not.
- A probe needs the user, a credential or production: ask once with the probe and its result, never guess past it.

## Habits that shorten the search

- Read the failure itself: `sampo fail` on the run, `sampo trace` on a stack, `sampo logs --since-good` on a log. The line you skim past is often the answer.
- The last thing that changed is a suspect, not a verdict: probe it, do not assume it.
- "It works on my machine" is a hypothesis about the environment: diff the two (versions, env vars, cwd, clock, locale).
- A flaky result is a finding: run it ten times, record the pass rate, then look for shared state, ordering, time and randomness.
- Say the answer to the person who asked in one sentence before the fix list: what was wrong, and how you know.

For the debug procedure itself (reproduce, narrow, fix, prove) use `sampo:debug-test`; this skill is how to think while you do it.
