import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

// T-6552: a headless session (claude -p: fm run workers, lane pilots, audit reviewers) has no pane or band to feed, so
// it runs no `fm ui --json` and arms no 2 s poll; each poll cost 0.1-1 s of CPU per worker, times ~100 workers.
function world(on: On) {
  const clock = mock.clock(on)
  mock.store(on)
  mock.env(on, { FOREMAN_FM: 'fm', HOME: '/home/u' })
  const runs: string[][] = []
  on('process.run', async (_$, e) => {
    runs.push(e.argv)
    return { value: { exitCode: 0, stdout: e.argv[1] === 'ui' ? '{}' : 'ok', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.panes', async () => ({ value: [] }))
  on('ui.open', async () => ({ value: undefined }))
  on('command.register', async (_$, e) => ({ value: { command: e.name } }))
  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  const uiRuns = () => runs.filter(a => a[1] === 'ui').length
  return { clock, uiRuns }
}

test('headless: a claude -p session runs no fm ui and arms no poll (T-6552)', async ($, on) => {
  const { clock, uiRuns } = world(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: false })
  await clock.advance(40000)
  expect(uiRuns()).toBe(0)
})

test('interactive: a session still refreshes and polls (T-6552)', async ($, on) => {
  const { clock, uiRuns } = world(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const first = uiRuns()
  expect(first).toBeGreaterThan(0)
  await clock.advance(40000)
  expect(uiRuns()).toBeGreaterThan(first)
})
