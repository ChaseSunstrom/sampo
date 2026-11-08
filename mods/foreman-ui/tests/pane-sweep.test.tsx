import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { FmView } from '../types'

// T-1050: the pane at many sizes with a busy view. The sweep reads the tree, not pixels: nothing fixed-width may be
// wider than the pane, and plain text wider than it must truncate or wrap rather than spill.
const LONG = 'a very long title that goes on and on well past any narrow pane so it has to be cut somewhere sensible'
const VIEW: FmView = {
  v: 1,
  project: 'demo-1a2b3c',
  root: '/repo',
  mode: { autonomy: 'standard', drive: true, sensitive: false },
  active: {
    id: 'T-0007', type: 'FIX', tier: 'M', title: LONG, status: 'active', stage: 'executing',
    stages: ['planning', 'ready', 'executing', 'verifying', 'auditing', 'closing'],
    steps: [
      { n: 1, text: LONG, done: true, current: false },
      { n: 2, text: LONG, done: false, current: true },
      { n: 3, text: LONG, done: false, current: false },
    ],
    criteria: [{ n: 1, text: LONG, verify: 'pytest -q ' + LONG, checked: false }],
    audits: { done: 0, need: 2 },
    blockers: [LONG],
    hypotheses: [{ n: 1, status: 'open', text: LONG }],
  },
  next: LONG,
  queue: [
    { id: 'T-0008', type: 'CLEAN', tier: 'S', title: LONG, status: 'planned', steps_done: 1, steps_total: 3 },
    {
      id: 'T-0009', type: 'FEATURE', tier: 'L', title: LONG, status: 'planned', waits: 'plan approval',
      plan: {
        interpretation: LONG, approach: LONG,
        steps: [{ n: 1, text: LONG, done: false, current: true }],
        criteria: [{ n: 1, text: LONG, verify: 'pytest -q', checked: false }],
      },
    },
  ],
  inbox: [{ id: 'T-0011', type: 'CLEAN', tier: 'S', title: LONG, status: 'captured', age_days: 2 }],
  inbox_total: 1,
  approvals: [],
  closed: [],
  recent: ['13:12 ⚑ T-0011 ' + LONG],
  health: { hook_p95_ms: 31, guard_blocks: 0, hook_errors: 0 },
  watch: [],
  latency: [20, 25, 31, 22, 40, 28],
  checks: { at: '2026-10-02T19:40:00Z', results: [{ cmd: LONG, exit: 1, s: 11.0, note: LONG }] },
  revisit: [{ date: '2026-09-01', decision: LONG, why: LONG }],
}

function world(on: On) {
  mock.clock(on)
  mock.store(on)
  mock.env(on, { FOREMAN_FM: 'fm', HOME: '/home/u' })
  on('process.run', async ($, e) => ({
    value: { exitCode: 0, stdout: e.argv[1] === 'ui' ? JSON.stringify(VIEW) : 'ok', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
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

// T-2293: 30 mounts take ~5 s of real CPU, the engine's default cap; a loaded host timed it out
test('the pane fits every width and height: nothing wider than the pane, nothing spills (T-1050)', { timeoutMs: 60000 }, async ($, on) => {
  world(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const bad: string[] = []
  for (const w of [30, 40, 60, 80, 120, 200])
    for (const h of [8, 12, 24, 40, 60]) {
      const ui = await $.ui.mount({
        plugin: 'sampo-ui', surface: 'terminal', component: 'Pane', requestId: 'foreman',
        props: { title: 'Sampo', isFocused: true, bodyColumns: w, placement: 'dock', scroll: { offset: 0, bodyRows: h }, view: {} },
      } as never)
      if (h >= 24 && !(await ui.find({ type: 'Text', text: /T-0007/ }))) bad.push(`${w}x${h} drew no task`) // the sweep sees a real pane
      for (const r of await ui.findAll({ type: 'Raster' })) if (Number(r.props.columns) > w) bad.push(`${w}x${h} raster ${r.key} ${r.props.columns}`)
      for (const b of await ui.findAll({ type: 'Box' })) if (typeof b.props.width === 'number' && b.props.width > w) bad.push(`${w}x${h} box ${b.key} ${b.props.width}`)
      for (const t of await ui.findAll({ type: 'Text' })) {
        const s = String(t.text ?? '')
        if ([...s].length > w && !String(t.props.wrap ?? '').startsWith('truncate') && t.props.wrap !== 'wrap') bad.push(`${w}x${h} text ${[...s].length}: ${s.slice(0, 40)}`)
      }
      await ui.unmount()
    }
  expect([...new Set(bad)]).toEqual([])
})
