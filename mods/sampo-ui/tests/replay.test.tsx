import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import REPLAY from './fixtures/replay'
import GOLDEN from './fixtures/golden'

// T-1534: the pane drawn from a recorded `fm ui --json` view with no live session: fm, the clock (frozen: it moves only
// when a test advances it), the store and the environment are fakes. tools/pane_replay.py runs this same file in a copy
// of the mod with its own fixtures/replay.ts and print: true, and diffs two runs, each its own process.
const MARK = 'FM-REPLAY '

function world(on: On) {
  mock.clock(on)
  mock.store(on)
  mock.env(on, { FOREMAN_FM: 'fm', HOME: '/home/u' })
  on('process.run', async ($, e) => ({
    value: { exitCode: 0, stdout: e.argv[1] === 'ui' ? JSON.stringify(REPLAY.view) : 'ok', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  on('ui.panes', async () => ({ value: [] }))
  on('session.id', async () => ({ value: 'sess-1' }))
  on('ui.open', async () => ({ value: undefined }))
  on('ui.toast', async () => ({ value: undefined }))
  on('ui.blit', async () => ({ value: undefined }))
  on('session.usage', async () => ({ value: { startedAt: 0, context: { window: 1000, percent: 30 }, rateLimits: [], cost: { usd: 1.5 } } }))
  on('agent.list', async () => ({ value: [] }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  on('tool.call', async () => ({ result: {} }))
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('ui.render', async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
}

const runs: string[] = []
// the drawn tree without the engine's callback ids (press.handle counts up across mounts): ids, not pixels
const frame = (tree: unknown) =>
  JSON.stringify(tree, (k, v) => (k === 'press' && v && typeof v === 'object' && 'handle' in v ? undefined : v))

type Kit = Parameters<Parameters<typeof test>[1]>[0] // the engine side a test drives

async function draw($: Kit, on: On) {
  world(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({
    plugin: 'sampo-ui', surface: 'terminal', component: 'Pane', requestId: 'foreman',
    props: { title: 'Sampo', isFocused: true, bodyColumns: REPLAY.columns, placement: 'dock', scroll: { offset: 0, bodyRows: REPLAY.rows }, view: {} },
  } as never)
  const drawn = frame(await ui.drawn())
  await ui.unmount()
  runs.push(drawn)
  return drawn
}

test('replay: the pane draws from a recorded view with no live session (T-1534)', async ($, on) => {
  const drawn = await draw($, on)
  if (REPLAY.print) console.log(MARK + drawn)
  expect(drawn).toContain('"key":"fm-pane"')
  if (REPLAY.view.project) expect(drawn).toContain(JSON.stringify(` ${REPLAY.view.project}`)) // the head (the top bar: after SAMPO), at any size
  if (REPLAY.rows >= 24 && REPLAY.view.active?.id) expect(drawn).toContain(REPLAY.view.active.id) // as the sweep (T-1050)
})

test('replay: a second run in a fresh world draws the same bytes (T-1534)', async ($, on) => {
  const drawn = await draw($, on)
  expect(runs.length).toBe(2)
  expect(drawn).toBe(runs[0]!)
})

// T-1670: the committed golden frame. A pane change that draws differently fails here until
// `python3 tools/pane_replay.py golden --update` rewrites fixtures/golden.ts (its diff shows what moved).
test('replay: the pane draws its golden frame, else tools/pane_replay.py golden --update (T-1670)', async ($, on) => {
  if (REPLAY.print) return // a tool's own copy draws another view: no golden there
  const drawn = await draw($, on)
  expect(drawn).toBe(GOLDEN)
})
