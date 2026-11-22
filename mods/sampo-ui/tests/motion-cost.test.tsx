import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { FmView } from '../types'
import GOLDEN_VIEW from './fixtures/pane-busy'

// T-5546: the mod's motion cost half a core per busy session: the pane and the band redrew 8 times a second for every
// moving glyph. Now what moves is repainted by blit (20 fps in full motion) and the pane and the band redraw about once
// a second. This counts the redraws of 10 s of live work on the mock clock (a redraw is a new tree from `ui.drawn()`),
// and checks every moving element still changes between the blits.
const PANE = {
  component: 'Pane',
  requestId: 'foreman',
  props: { title: 'Sampo', isFocused: true, bodyColumns: 104, placement: 'dock', scroll: { offset: 0, bodyRows: 120 }, view: {} },
} as const
const BAND = {
  component: 'AbovePrompt',
  requestId: 'band-1',
  props: { hasSurvey: false, isWorking: true, maxRows: 14, bodyColumns: 120, scroll: { offset: 0, bodyRows: 14 }, view: {} },
} as const

function world(on: On, view: unknown, gate = false) {
  const clock = mock.clock(on)
  mock.store(on, { page: 'details' })
  mock.env(on, { FOREMAN_FM: 'fm', HOME: '/home/u' })
  on('process.run', async (_$, e) => ({
    value: { exitCode: 0, stdout: e.argv[1] === 'ui' ? JSON.stringify(view as FmView) : 'ok', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  on('ui.panes', async () => ({ value: [] }))
  on('session.id', async () => ({ value: 'sess-1' }))
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('ui.toast', async () => ({ value: undefined }))
  const blits: { key: string; requestId: string; cells: string }[] = []
  on('ui.blit', async (_$, e) => {
    const b = e as unknown as { key: string; requestId: string; cells: string }
    blits.push({ key: b.key, requestId: b.requestId, cells: b.cells })
    return { value: {} }
  })
  on('session.usage', async () => ({ value: { startedAt: 0, context: { window: 1000, percent: 30 }, rateLimits: [], cost: { usd: 1.5 } } }))
  on('agent.list', async () => ({ value: [] }))
  on('command.register', async (_$, e) => ({ value: { command: e.name } }))
  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', async (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', async () => ({ text: '' }))
  const gates: (() => void)[] = [] // gate: a call stays running until its gate is opened
  on('tool.call', async () => {
    if (gate) await new Promise<void>(r => gates.push(r))
    return { result: {} }
  })
  on('ui.render', async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  return { clock, blits, gates }
}

const STEP = 50
const TEN_SECONDS = 10000

async function run(on: On, $: Parameters<Parameters<typeof test>[2]>[0], mount: typeof PANE | typeof BAND) {
  const { clock, blits } = world(on, GOLDEN_VIEW)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'go', turnId: 't1' }) // a turn in flight: live work for the whole run
  await $.tool.call({ tool: 'Bash', command: 'make build', description: 'build' })
  await clock.advance(STEP)
  const ui = await $.ui.mount({ plugin: 'sampo-ui', surface: 'terminal', ...mount } as never)
  let tree = JSON.stringify(await ui.drawn())
  let redraws = 0
  const blitsBefore = blits.length
  for (let t = 0; t < TEN_SECONDS; t += STEP) {
    await clock.advance(STEP)
    const now = JSON.stringify(await ui.drawn()) // the engine draws what changed; a redraw puts the draw's own time in the rasters
    if (now !== tree) redraws++
    tree = now
  }
  const painted = blits.slice(blitsBefore)
  await ui.unmount()
  return { redraws, painted }
}

test('motion full: 10 s of live work redraws the pane about once a second while the blits keep painting (T-5546)', { timeoutMs: 120000 }, async ($, on) => {
  const { redraws, painted } = await run(on, $, PANE)
  console.log(`FM-RENDERS pane=${redraws} blits=${painted.length}`)
  expect(redraws).toBeLessThanOrEqual(12)
  expect(painted.length).toBeGreaterThan(100) // 200 ticks at 50 ms across the pane's moving pieces
  expect(painted.every(b => b.requestId === 'foreman')).toBe(true)
})

test('motion full: every moving element of the pane changes between the blits (T-5546)', { timeoutMs: 120000 }, async ($, on) => {
  const { painted } = await run(on, $, PANE)
  const variants = new Map<string, Set<string>>()
  for (const b of painted) variants.set(b.key, (variants.get(b.key) ?? new Set()).add(b.cells))
  console.log(`FM-MOVING ${[...variants].map(([k, s]) => `${k}:${s.size}`).join(' ')}`)
  for (const key of ['stage-rail', 'head-mark', 'fm-crest', 'task-steps'])
    expect(variants.get(key)?.size ?? 0, key).toBeGreaterThan(1)
})

test('motion full: no key blits faster than the 50 ms clock (T-5546)', { timeoutMs: 120000 }, async ($, on) => {
  const { painted } = await run(on, $, PANE)
  const per = new Map<string, number>()
  for (const b of painted) per.set(b.key, (per.get(b.key) ?? 0) + 1)
  console.log(`FM-CEILING ${[...per].map(([k, n]) => `${k}:${n}`).join(' ')}`)
  for (const [k, n] of per) expect(n, k).toBeLessThanOrEqual(TEN_SECONDS / 50 + 2)
})

test('motion reduced: the pane redraws about once a second and the blit runs at 8 fps (T-5546)', { timeoutMs: 120000, options: { motion: 'reduced' } }, async ($, on) => {
  const { redraws, painted } = await run(on, $, PANE)
  const per = new Map<string, number>()
  for (const b of painted) per.set(b.key, (per.get(b.key) ?? 0) + 1)
  console.log(`FM-RENDERS reduced pane=${redraws} blits=${painted.length}`)
  expect(redraws).toBeLessThanOrEqual(12)
  expect(painted.length).toBeGreaterThan(0)
  for (const [k, n] of per) expect(n, k).toBeLessThanOrEqual(TEN_SECONDS / 125 + 2)
})

test('motion off: nothing blits and the pane redraws only on the poll (T-5546)', { timeoutMs: 120000, options: { motion: 'off' } }, async ($, on) => {
  const { redraws, painted } = await run(on, $, PANE)
  console.log(`FM-RENDERS off pane=${redraws} blits=${painted.length}`)
  expect(painted.length).toBe(0)
  expect(redraws).toBeLessThanOrEqual(6) // the 2 s poll, five times in 10 s
})

// T-5546 review: a call that starts or ends soon after a redraw is drawn at the throttle's end, not on the next 1 s check
test('a tool starting 100 ms after another is drawn within 300 ms (T-5546)', { timeoutMs: 120000 }, async ($, on) => {
  const { clock, gates } = world(on, GOLDEN_VIEW, true)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'go', turnId: 't1' })
  const ui = await $.ui.mount({ plugin: 'sampo-ui', surface: 'terminal', ...PANE } as never)
  await clock.advance(1500)
  void $.tool.call({ tool: 'Bash', command: 'make build', description: 'build' })
  await clock.advance(100)
  void $.tool.call({ tool: 'Read', file_path: '/repo/zeta-unique.py' } as never)
  await clock.advance(300)
  expect(JSON.stringify(await ui.drawn())).toContain('zeta-unique')
  for (const open of gates) open()
  await clock.advance(1500)
  await ui.unmount()
})

test('motion off: 20 tool events in 2 s redraw the pane at most twice (T-5546)', { timeoutMs: 120000, options: { motion: 'off' } }, async ($, on) => {
  const { clock } = world(on, GOLDEN_VIEW)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ plugin: 'sampo-ui', surface: 'terminal', ...PANE } as never)
  await clock.advance(50)
  let tree = JSON.stringify(await ui.drawn())
  let redraws = 0
  for (let i = 0; i < 20; i++) {
    await $.tool.call({ tool: 'Read', file_path: `/repo/off-${i}.py` } as never)
    await clock.advance(100)
    const now = JSON.stringify(await ui.drawn())
    if (now !== tree) redraws++
    tree = now
  }
  console.log(`FM-RENDERS off-tools pane=${redraws}`)
  expect(redraws).toBeLessThanOrEqual(2)
  await ui.unmount()
})

test('motion full: 10 s of live work redraws the band about once a second and its bars and spinners blit (T-5546)', { timeoutMs: 120000 }, async ($, on) => {
  const { redraws, painted } = await run(on, $, BAND)
  const keys = [...new Set(painted.map(b => b.key))]
  console.log(`FM-RENDERS band=${redraws} blits=${painted.length} keys=${keys.join(',')}`)
  expect(redraws).toBeLessThanOrEqual(12)
  expect(painted.length).toBeGreaterThan(100)
  expect(painted.every(b => b.requestId === 'band-1')).toBe(true)
})
