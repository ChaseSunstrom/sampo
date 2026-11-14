---
name: concurrency-tests
title: Concurrency tests
area: testing
tags: [testing, concurrency, race, threads, locks, deadlock, stress]
detect: []
related: [async-tests, flaky-tests, test-isolation, chaos-testing]
---
Prove thread-safety by forcing contention: many workers, barriers, race detectors and repeated runs, never a single lucky pass.

## Verify
- `go test -race -count=50 ./pkg/...`: the race detector over many repetitions finds data races (Go).
- `cargo test` with `loom` for core primitives: exhaustively explores thread interleavings of the model (Rust).
- `python -X dev -m pytest -q -x --count=50`: repeated runs with dev-mode checks (needs pytest-repeat).
- `java -ea -jar` with a stress tool such as jcstress or `mvn test -Dsurefire.rerunFailingTestsCount=0`: no retries hide failures (JVM).
- `timeout 120 pytest -q tests/concurrency`: a deadlock fails the run instead of hanging. macOS has no `timeout`: `gtimeout` (brew coreutils) or the portable `pytest -q --timeout=120 tests/concurrency` (pytest-timeout).

## Test first
Start many workers at the same instant and assert the invariant.
```go
func TestCounterIsAtomic(t *testing.T) {
	var c Counter
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); <-start; for j := 0; j < 1000; j++ { c.Inc() } }()
	}
	close(start)
	wg.Wait()
	if got := c.Value(); got != 100000 { t.Fatalf("got %d", got) }
}
```
Run it with `-race`. It fails on an unsynchronized counter.

## Do
- Release workers together with a barrier or closed channel so contention is real.
- Assert invariants (totals, uniqueness, ordering guarantees, no lost update), not timing.
- Run each test many times and under a race or thread sanitizer in CI (`-race`, TSan).
- Use more workers than cores, and vary counts, to expose scheduling-dependent bugs.
- Add deadlock detection through timeouts on every join, lock acquire and test.
- Test lock ordering, cancellation and shutdown while work is in flight.
- Keep shared test state minimal; each worker uses its own ids.
- Make the design testable: inject the clock and the executor, and expose synchronization points.

## Avoid
- `sleep` to "let the other thread run": passes by luck. Use barriers, latches and joins.
- A single run as proof: races appear rarely. Repeat many times with a detector.
- Asserting on thread scheduling order: nondeterministic. Assert outcomes the design guarantees.
- Retrying a failing concurrency test until green: the race stays. Reproduce it and fix the synchronization.
- Joining without a timeout: a deadlock hangs CI. Always bound.
- Test-only locks or global mutexes that serialize the code under test: the race is hidden. Test the real code path.

## Review
- [ ] Workers start together and count exceeds cores.
- [ ] A race detector or sanitizer runs in CI.
- [ ] Assertions are invariants, not timing.
- [ ] Every wait has a timeout.
- [ ] Tests repeat enough to find intermittent races.
