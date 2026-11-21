---
name: cc-mod-testing
title: Testing UI mods
area: claude-code
tags: [claude-code, mods, testing, tsx, harness, clock]
detect: [file:tests/*.test.tsx]
related: [cc-mods-ui, cc-plugin-testing, cc-plugin-evals]
---
Mods are tested with `claude plugin test` using the `claude-code/testing` harness: pure helpers as plain tests, event flows through a simulated engine with a fake clock.

## Verify
- `claude plugin test <mod-dir>`: runs every test file and prints a pass and fail summary.
- `claude plugin test <mod-dir>` after editing a helper only: the cheap loop for a pure change.
- `claude plugin test mods/sampo-ui`: this repo's UI mod. It has no package.json, so never `npx vitest run` in it: `claude-code/testing` resolves only under `claude plugin test`.
- `npx tsc --noEmit -p <mod-dir>`: the tests type-check against the engine declarations.

## Test first
Pure helper first, then a flow test with the harness.
```tsx
import { expect, mock, test } from 'claude-code/testing'
import { toasts } from '../hooks/register'
test('toasts: a task closing yields one toast', () => {
  expect(toasts(BEFORE, AFTER)).toContain('done T-0009')
})
test('/fm-trust refuses a scheduled origin', async ($, on) => {
  register(on, {})
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const r = await $.command.run({ command: 'fm-trust', args: 'on', origin: { kind: 'scheduled-trigger' }, presentation: { isFullscreen: false, columns: 120 } })
  expect(r).toBeDefined()
})
```

## Do
- Imports: `import { expect, mock, test } from 'claude-code/testing'`; flow tests take `($, on)`.
- Build test worlds with a helper that stubs the engine hooks your mod calls (`on('fs.write', ...)`, command and process handlers) and records calls.
- Drive time with the fake clock (`await clock.advance(2000)`) rather than sleeping.
- Assert on the rendered tree: `await pane.find({ type: 'Text', text: /regex/ })`, press keyed controls with `pane.press({ key })`, and `await pane.unmount()` at the end.
- Test the privileged edges: who may run a command (origin), and what gets written.
- Put snapshot-like fixtures (a sample view object) at the top and reuse them.
- Keep one test per behavior with a name that states it.

## Avoid
- Real sleeps and real timers: slow and flaky. Advance the fake clock.
- Asserting on layout strings that change with width: set `presentation.columns` and match stable text with regexes.
- Testing only helpers: handler wiring (forgetting `next(e)`) goes unseen. Add flow tests.
- Stubs that return nothing for calls the mod awaits: the test hangs or passes vacuously. Return the real shape.
- Shared mutable fixtures between tests: copy per test.
- Leaving a pane mounted: later tests see stale timers. Unmount.

## Review
- [ ] Pure helpers and event flows are both covered.
- [ ] Time is controlled by the fake clock.
- [ ] Privileged commands are tested for refusal.
- [ ] Each test unmounts what it mounts.
- [ ] `claude plugin test` is green with no skipped tests.
