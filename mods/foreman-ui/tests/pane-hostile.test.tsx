import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { FmView } from '../types'

// T-1679: test_code_tui.HostileWidth's strings through the pane's own renderer. Each string fills every text slot of a
// busy view; at each size nothing fixed is wider than the pane, plain text wider than it (in columns, wide glyphs 2)
// truncates or wraps, and no drawn text holds an escape, a control or a bidi override.
const HOSTILE: Record<string, string> = {
  cjk: '日本語テキスト漢字かな', emoji: '🚀🔥 ok 👍🏽 ❤️ ✔️', combining: 'éé̂ ạ̈ ́lead',
  rtl: 'שלום עולם مرحبا ‮abc‬', zerowidth: 'a​b‍c⁠d﻿e­f',
  control: 'a\u0000b\u0007c\u001b[31md\u007fe\u0085f\r\n\tg', variation: 'x️y︎ ❤️', mix: '日́本🚀‍🔥 שׁ',
  long: '日本語🚀'.repeat(60),
}
const BAD = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f‪-‮⁦-⁩]/

/** Columns a terminal gives s: East Asian wide and emoji 2, marks and format characters 0 (a test's own rule). */
function cols(s: string): number {
  let n = 0
  for (const ch of s) {
    const c = ch.codePointAt(0)!
    if (/\p{M}|\p{Cf}/u.test(ch)) continue
    n += (c >= 0x1100 && c <= 0x115f) || (c >= 0x2e80 && c <= 0xa4cf) || (c >= 0xac00 && c <= 0xd7a3) ||
      (c >= 0xf900 && c <= 0xfaff) || (c >= 0xfe30 && c <= 0xfe4f) || (c >= 0xff00 && c <= 0xff60) ||
      (c >= 0xffe0 && c <= 0xffe6) || (c >= 0x1f300 && c <= 0x1faff) || (c >= 0x20000 && c <= 0x3fffd) ? 2 : 1
  }
  return n
}

function view(s: string): FmView {
  return {
    v: 1, project: s, root: '/repo',
    mode: { autonomy: 'standard', drive: true, sensitive: false },
    active: {
      id: 'T-0007', type: 'FIX', tier: 'M', title: s, status: 'active', stage: 'executing',
      stages: ['planning', 'ready', 'executing', 'verifying', 'auditing', 'closing'],
      steps: [{ n: 1, text: s, done: true, current: false }, { n: 2, text: s, done: false, current: true }],
      criteria: [{ n: 1, text: s, verify: s, checked: false }],
      audits: { done: 0, need: 2 }, blockers: [s], hypotheses: [{ n: 1, status: 'open', text: s }],
    },
    next: s,
    queue: [{ id: 'T-0008', type: 'CLEAN', tier: 'S', title: s, status: 'planned', steps_done: 1, steps_total: 3 }],
    inbox: [{ id: 'T-0011', type: 'CLEAN', tier: 'S', title: s, status: 'captured', age_days: 2 }],
    inbox_total: 1, approvals: [], closed: [], recent: ['13:12 ⚑ T-0011 ' + s],
    health: { hook_p95_ms: 31, guard_blocks: 0, hook_errors: 0 }, watch: [], latency: [20, 25, 31],
    checks: { at: '2026-10-02T19:40:00Z', results: [{ cmd: s, exit: 1, s: 11.0, note: s }] },
    revisit: [{ date: '2026-09-01', decision: s, why: s }],
  } as FmView
}

function world(on: On, now: FmView) {
  mock.clock(on)
  mock.store(on)
  mock.env(on, { FOREMAN_FM: 'fm', HOME: '/home/u' })
  on('process.run', async ($, e) => ({
    value: { exitCode: 0, stdout: e.argv[1] === 'ui' ? JSON.stringify(now) : 'ok', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
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
}

for (const [name, s] of Object.entries(HOSTILE))
  test(`hostile ${name} text fits every size and draws no control, escape or bidi override (T-1679)`, async ($, on) => {
    world(on, view(s))
    await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
    const bad: string[] = []
    for (const [w, h] of [[20, 12], [26, 12], [40, 16], [80, 24], [121, 34]] as const) {
      const ui = await $.ui.mount({
        plugin: 'sampo-ui', surface: 'terminal', component: 'Pane', requestId: 'foreman',
        props: { title: 'Sampo', isFocused: true, bodyColumns: w, placement: 'dock', scroll: { offset: 0, bodyRows: h }, view: {} },
      } as never)
      if (w >= 26 && h >= 24 && !(await ui.find({ type: 'Text', text: /T-0007/ }))) bad.push(`${w}x${h} drew no task`)
      if (w < 26 && !(await ui.find({ type: 'Text', text: /26\+ columns/ }))) bad.push(`${w}x${h} said nothing of its floor`)
      for (const r of await ui.findAll({ type: 'Raster' })) if (Number(r.props.columns) > w) bad.push(`${w}x${h} raster ${r.key} ${r.props.columns}`)
      for (const b of await ui.findAll({ type: 'Box' })) if (typeof b.props.width === 'number' && b.props.width > w) bad.push(`${w}x${h} box ${b.key} ${b.props.width}`)
      // a span inside a truncating Text is cut with it: only a Text no cut parent holds must fit on its own
      const texts = (await ui.findAll({ type: 'Text' })).map(t => ({ text: String(t.text ?? ''), wrap: String(t.props.wrap ?? '') }))
      const cut = (x: { wrap: string }) => x.wrap.startsWith('truncate') || x.wrap === 'wrap'
      for (const t of texts) {
        if (BAD.test(t.text)) bad.push(`${w}x${h} drew ${JSON.stringify(t.text.slice(0, 40))}`)
        if (cols(t.text) > w && !cut(t) && !texts.some(u => u !== t && cut(u) && u.text.includes(t.text)))
          bad.push(`${w}x${h} text ${cols(t.text)} cols: ${t.text.slice(0, 30)}`)
      }
      await ui.unmount()
    }
    expect([...new Set(bad)]).toEqual([])
  })
