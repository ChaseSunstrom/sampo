# Critique — Mission 1, exhaustive thinking (gen 4, #2)

Tasks: T-2910 (done, merged 2026-11-04 09:04Z). T-2921 (captured, the live head-to-head). T-2878 step 2 (planned, the wider volume work). Mission: gen-4/mission-exhaustive-thinking.md.

## Closed vs planned
- Brief 1 (T-2910) is closed as code. `sampo ideas --model local` sends every lens, deepen, falsify and crossbreed child through moonscale's routing: own or loopback profile first, Haiku as the fallback, and the answering model booked per child in manifest.json. A sensitive project asks loopback only and fails closed. Criteria 1 and 2 are green on test_ideas (30 tests).
- Review chose the right route. The builder did not copy the routing. It reused `moonscale._ask`, and the task's lesson says why: one router that knows sensitivity and fallbacks, never a second copy.
- Criterion 3 was not met. It was the head-to-head: the gen-4 pack on the local profile and on sonnet, 40 sampled ideas per arm, a blind cold judge. At 07:32Z it was moved to T-2921 because both model hosts were busy that night. T-2921 is still captured, with no steps and no acceptance criteria written. Nothing has run.
- The live reading the Mission asked for before close (one `--model local --rounds 1` run whose manifest shows the local profile on every child) is not in the task's evidence. The evidence is test runs only. The closing log has no live run.
- One of the task's own checks failed at the end: `run.py test_ideas_local` exited 1, because no test file has that name. It was a wrong command, not a wrong result. It still sat in the evidence unexplained at close.

## Measures (first readings and due dates)
- Judged-good count, local vs sonnet, against the 80% and 50% thresholds. First reading: none. The run does not exist. No due date can be honest while T-2921 has no criteria and the endpoint is busy. Due: when T-2921 runs; the next generation should set a date.
- Default flip. The default stays sonnet. The decision rule needs the number above, so it cannot fire.
- Use in practice. The ledger holds three `ideas` events after the 09:04Z merge. The newest (14:18Z, 235 ideas, 2 rounds) has `"model": "sonnet"` in its manifest. So the arm exists and the next brainstorm on this machine still paid for the cloud. Nothing in the procedure picks local.
- The premise reading moved. The Mission quoted `sampo moons coverage` on 2026-11-04: "haiku 545 of 1499, a large local model+nothink 2753 of 3316, a large local model+think 64 of 64". The same command now reads "haiku 134 of 1499, a large local model+nothink 157 of 8444, a large local model+think 1 of 64". Local kept rate is 1.9% against Haiku's 8.9%. The two readings may count different things (the funnel may have tightened, and the denominators changed). I cannot say which from the output. But the claim "the local 27B's candidates survive the cheap filters far more often" is not true on today's reading, and the Mission's case rested on it.

## What failed
- The Mission's one outcome measure was deferred in the same hour the builder started. A Mission whose Exam is a comparison, and whose comparison needs the same scarce endpoint the night's other jobs use, should have been seeded with that dependency named and a slot booked. Instead the brief closed on tests and the measure became a captured stub.
- The width itself is untested. The task shows no run in which local children produced ideas a judge rated. A 27B with reasoning off may drift from the lens file's bullet shape, which the parser drops. That was the stated 25% doubt, and it is still a doubt.
- The Mission's capability line says "width that costs nothing". Today's evidence shows only that the switch exists.

## What the next Seed should change
- Do not close a Mission brief on criteria that leave out its Exam. If the Exam must wait, the brief stays open or the follow-up is filed with criteria, a verify command and a date, not as a one-line capture.
- Book the endpoint. Genesis should reserve a slot window for the Exam runs of the generation (the other jobs are moons growth and the GPU exam), or run the head-to-head on the GPU profile that is free.
- Re-read the premise number on the day the brief is cut. The moons kept-rate flipped within a day. A brief that rests on a rate quotes it with its denominators and the command, so the next reader can reproduce it.
- Make the procedure use what was built. The brainstorm skill should try `--model local` when a profile is up, and say when it did not. Otherwise T-2910 is a flag nobody passes.
- Next Mission 1 brief: run T-2921 first. If local reaches 80% of sonnet's good count, make it the default for lens children only. If it lands under 50%, take the failure shape (format drift, repeats, ungrounded ideas) as the lead.
