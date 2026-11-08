import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { canvas, cpuPicture, lanesPicture, libraryPicture, mergePicture, modelsPicture, narrow, sampoPicture, sessionsPicture, statePicture,
  taskPicture, testsPicture } from '../hooks/canvas'
import { C, gridRows, mix, setPalette } from '../hooks/kit'
import type { FmView } from '../types'
import BUSY from './fixtures/board-busy'
import TASK from './fixtures/board-task'

// T-3701 (rework): the pane's board page, a top bar over the live page Board's nine panels and SESSIONS in a grid, each a picture drawn in
// pixels (half blocks, two a cell; braille for fine dots and hairlines).

const decode = (b64: string) => {
  const bin = atob(b64)
  const view = new DataView(new Uint8Array([...bin].map(c => c.charCodeAt(0))).buffer)
  const out: { ch: string; fg: number; bg: number }[] = []
  for (let i = 0; i < bin.length; i += 12)
    out.push({ ch: String.fromCodePoint(view.getUint32(i, true)), fg: view.getUint32(i + 4, true), bg: view.getUint32(i + 8, true) })
  return out
}
const textOf = (cells: { ch: string }[]) => cells.map(c => c.ch).join('')
/** a pixel (a ▀ cell's or a full cell's) of about this colour: drawn translucent, a fill is the colour over the ground */
const near = (a: number, b: number) => [16, 8, 0].every(s => Math.abs(((a >> s) & 255) - ((b >> s) & 255)) <= 40)
const painted = (cells: { ch: string; fg: number; bg: number }[], color: number) =>
  cells.some(c => (c.ch === '▀' || c.ch === ' ') && (near(c.fg, color) || near(c.bg, color)))

test('canvas: two pixels a cell as a half block, words over them, rounded boxes in cells', () => {
  setPalette('default')
  const cv = canvas(3, 2)
  cv.dot(0, 0, 0xff0000)
  cv.dot(0, 1, 0x00ff00)
  cv.write(1, 1, 'ab', 0x123456)
  const drawn = decode(cv.cells())
  expect(drawn[0]).toEqual({ ch: '▀', fg: 0xff0000, bg: 0x00ff00 }) // the top pixel the glyph, the bottom one the ground
  expect(drawn[1]).toEqual({ ch: ' ', fg: C.bg, bg: C.bg }) // the ground
  expect([drawn[4]!.ch, drawn[5]!.ch, drawn[4]!.fg]).toEqual(['a', 'b', 0x123456])
  const box = canvas(4, 3)
  box.box(0, 0, 4, 3, 0xffffff)
  expect(textOf(decode(box.cells()))).toBe('╭──╮│  │╰──╯')
  expect(narrow('a日‮b́')).toBe('a·b') // a wide glyph one cell, no bidi override or mark
})

test('canvas: lines and curves reach both ends; a trapezoid fills between its edges; a glow fades out', () => {
  setPalette('default')
  const cv = canvas(10, 5)
  cv.curve([[0, 0], [5, 0], [5, 9], [9, 9]], 0xff0000)
  cv.trap(2, 6, 1, 8, 4, 5, 0x00ff00, 1, 0x0000ff)
  const px = (x: number, y: number) => { const c = decode(cv.cells())[Math.floor(y / 2) * 10 + x]!; return y % 2 ? (c.ch === '▀' ? c.bg : c.fg) : c.ch === '▀' ? c.fg : c.bg }
  expect(px(0, 0)).toBe(0xff0000)
  expect(px(9, 9)).toBe(0xff0000)
  expect(px(4, 4)).toBe(0x00ff00) // inside the gate
  expect([px(2, 1), px(2, 8), px(6, 4), px(6, 5)]).toEqual([0x0000ff, 0x0000ff, 0x0000ff, 0x0000ff]) // its outline: each side whole
  const g = canvas(9, 5)
  g.glow(4, 5, 4, 0xffffff, 1)
  const cells = decode(g.cells())
  expect(cells[2 * 9 + 4]!.fg).not.toBe(C.bg) // lit at the centre
  expect(cells[0]!.fg).toBe(C.bg) // nothing at a corner
})

const MERGE = { waiting: [{ id: 'T-0023', title: 'Tidy the date helpers', color: 0xffaa00, frac: 1 }, { id: 'T-0024', title: 'Cache the view', color: 0x9d8cff, frac: 0.5 }],
  landed: [{ id: 'T-0015', at: '2026-01-02T08:05:00Z' }, { id: 'T-0016', at: '2026-01-02T09:41:00Z' }], landed24: 2, running: false }
/** an ISO time as this machine's HH:MM: what a landed row and a state entry show */
const local = (iso: string) => { const t = new Date(iso); return `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}` }

test('MERGE TRAIN: lane boxes pour into the gate, out into foreman/build; what landed, a ✓ and its time a row; packets while a train runs', () => {
  setPalette('default')
  const still = mergePicture(70, 16, MERGE)
  expect(still.live).toBeNull() // no train: a still picture
  const text = textOf(decode(still.cells))
  for (const s of [' T-0023 ', 'Tidy the date helpers', 'gate', 'foreman/build', '2 landed · 24 h', `✓ T-0016`, local('2026-01-02T09:41:00Z'), '✓ T-0015'])
    expect([s, text.includes(s)]).toEqual([s, true])
  expect(text.indexOf('✓ T-0016')).toBeLessThan(text.indexOf('✓ T-0015')) // the newest first
  expect(text).not.toContain('GATES')
  const gold = decode(still.cells).map((c, i) => [i % 70, Math.floor(i / 70), (c.ch === '▀' || c.ch === ' ') && (c.fg === C.accent || c.bg === C.accent)] as const)
    .filter(([, , on]) => on)
  const span = (k: 0 | 1) => new Set(gold.map(g => g[k])).size
  expect([span(0) >= 8, span(1) >= 4]).toEqual([true, true]) // the gate: a funnel about as wide as it is tall, outlined in gold
  const tight = textOf(decode(mergePicture(43, 15, MERGE).cells)) // a panel 43 wide: titles on two lines, whole words
  for (const s of ['Tidy the', 'date helpers', 'foreman/build', '2 landed · 24 h']) expect([s, tight.includes(s)]).toEqual([s, true])
  const many = { ...MERGE, landed: Array.from({ length: 9 }, (_, i) => ({ id: `T-00${30 + i}`, at: '2026-01-02T09:00:00Z' })), landed24: 9 }
  expect(textOf(decode(mergePicture(70, 14, many).cells))).toMatch(/\+\d more/) // more than its rows: the last says how many
  const run = mergePicture(70, 16, { ...MERGE, running: true })
  expect(textOf(decode(run.cells))).toContain('gate · running')
  expect(run.live!(1.0)).not.toBe(run.live!(1.5)) // the packets moved
  expect(run.live!(1.01)).toBe(run.live!(1.09)) // within a tenth of a second: the same frame, drawn once
  const ms: number[] = []
  for (let i = 0; i < 20; i++) {
    const t = performance.now()
    run.live!(2 + i / 10)
    ms.push(performance.now() - t)
  }
  const low = [...ms].sort((a, b) => a - b)[5]! // the lower quartile: a loaded host adds, never takes away
  console.log(`FM-FRAME merge packets ${low.toFixed(3)} ms (median ${[...ms].sort((a, b) => a - b)[10]!.toFixed(3)})`)
  expect(low).toBeLessThan(2) // the frame budget
})

test('LANES: a hub a lane, its files as named leaves; a file two lanes hold linked dotted; state and steps under; the next lit', () => {
  setPalette('default')
  const lanes = [
    { id: 'T-0021', title: 'Live page chart legend', color: C.accent2, done: 2, total: 5, files: ['plugin/web/live/board.js', 'plugin/web/live/live.js'], state: 'building', next: false },
    { id: 'T-0024', title: 'Cache the view build', color: C.accent, done: 4, total: 4, files: ['plugin/lib/sampo/live.py', 'plugin/web/live/live.js'], state: 'waiting', next: true },
  ]
  const pic = lanesPicture(70, 16, lanes)
  const drawn = decode(pic.cells)
  const text = textOf(drawn)
  for (const s of [' T-0021 ', 'live page chart', ' T-0024 ', 'board.js', 'live.js', 'live.py', 'building 2/5', 'waiting 4/4'])
    expect([s, text.includes(s)]).toEqual([s, true])
  expect(drawn[text.indexOf('live.js')]!.fg).toBe(C.ink) // a file both hold: lit, and listed first
  const fine = drawn.filter(c => c.ch >= '⠁' && c.ch <= '⣿')
  expect(text).toContain('⠤ ⠤ ⠤') // the dotted link between their live.js leaves
  expect(fine.filter(c => near(c.fg, C.accent2)).length).toBeGreaterThan(1) // the spokes, hairlines
  expect(drawn.some(c => c.fg === 0xffffff || c.bg === 0xffffff)).toBe(true) // the next to merge: a bright core
  expect(pic.live!(1)).not.toBe(pic.live!(1.5)) // a building lane writes: a packet down its spokes
  const many = [{ ...lanes[0]!, files: Array.from({ length: 12 }, (_, i) => `src/f${i}.py`) }]
  expect(textOf(decode(lanesPicture(40, 12, many).cells))).toMatch(/\+\d+ more/) // more files than rows: how many more
  expect(textOf(decode(lanesPicture(70, 16, lanes).cells))).toBe(text) // the same picture every draw
})

const LOOP = { id: 'T-0007', stage: 'verifying', stages: ['planning', 'ready', 'executing', 'verifying', 'auditing', 'closing'],
  title: 'Login times out on slow links', words: 'the login keeps timing out on slow links: retry once and say so',
  criteria: [{ text: 'a slow login retries once', ok: true }, { text: 'the message names the retry', ok: false }],
  steps: [{ text: 'reproduce the timeout', done: true, current: false }, { text: 'retry once', done: false, current: true }], files: [], done: 1, total: 2, time: '25m' }

test('THE LOOP: what you asked ❯❯❯ the brief in words ([x]/[ ] criteria, ✓/▸ steps), over the progress and the stages', () => {
  setPalette('default')
  const drawn = decode(taskPicture(76, 16, LOOP).cells)
  const text = textOf(drawn)
  for (const s of ['YOU ASKED', '“the login keeps', 'T-0007.md · VERIFYING', '# Login times out on slow links', '[x] a slow login retries once',
    '[ ] the message names the retry', '✓ 1. reproduce the timeout', '▸ 2. retry once', '1/2 steps · 25m', 'captured', 'verifying', 'closing'])
    expect([s, text.includes(s)]).toEqual([s, true])
  expect(text).not.toContain('no edits yet') // nothing changed: no box for it
  const fg = (s: string) => drawn[text.indexOf(s)]!.fg
  expect([fg('[x] a slow'), fg('[ ] the message'), fg('▸ 2.')]).toEqual([C.ok, C.accent, C.edit]) // met green, worked on amber, the step pink
  expect(textOf(decode(taskPicture(76, 16, { ...LOOP, files: [{ add: 30, del: 4 }] }).cells))).toContain('● 1 file +30 −4')
  const narrowText = textOf(decode(taskPicture(44, 14, LOOP).cells)) // too narrow for the card: the words as one line in the brief
  expect([narrowText.includes('YOU ASKED'), narrowText.includes('“the login')]).toEqual([false, true])
})

const CPU = { cores: 16, goal: 10, kinds: ['tests', 'claude', 'models', 'hooks', 'other'],
  now: { tests: 0.6, claude: 1.4, models: 0.9, hooks: 0.1, other: 1.5, busy: 4.5 },
  minutes: Array.from({ length: 30 }, (_, i) => [i * 60, 0.5, 1, 0.8, 0.1, 1 + (i % 5) / 10]) }

test('CPU: the cores busy as a big figure, its share against the goal, the kinds; a stacked bar a minute of the last 30', () => {
  setPalette('default')
  const pic = cpuPicture(70, 16, CPU)
  const drawn = decode(pic.live!(0))
  const text = textOf(drawn)
  for (const s of ['cores busy of 16', '28% · over goal', '■ tests', 'claude', 'models', 'hooks', 'other', '1.4', '10%', '−30 min', 'now'])
    expect([s, text.includes(s)]).toEqual([s, true])
  expect([painted(drawn, C.accent), painted(drawn, C.edit), painted(drawn, C.accent2)]).toEqual([true, true, true]) // the kinds stacked
  expect(painted(drawn, C.ink)).toBe(true) // the big figure, in pixels
  expect(drawn[text.indexOf('28% · over')]!.fg).toBe(C.warn) // over the goal: warm
  const calm = textOf(decode(cpuPicture(70, 16, { ...CPU, now: { ...CPU.now, busy: 1 } }).live!(0)))
  expect(calm).toContain('6.3% · under goal')
  expect(pic.live!(0)).not.toBe(pic.live!(0.5)) // the goal line marches
  const ms: number[] = []
  for (let i = 0; i < 20; i++) {
    const t = performance.now()
    pic.live!(3 + i / 10)
    ms.push(performance.now() - t)
  }
  const low = [...ms].sort((x, y) => x - y)[5]!
  console.log(`FM-FRAME cpu goal ${low.toFixed(3)} ms`)
  expect(low).toBeLessThan(2) // the frame budget: it moves whenever it shows
  expect(textOf(decode(cpuPicture(30, 12, CPU).live!(0)))).toContain('4.5 cores busy') // narrow: the figure in words
  const two = (gap: number) => cpuPicture(70, 16, { ...CPU, minutes: [[600 - gap * 60, 1, 1, 1, 0, 1], [600, 1, 1, 1, 0, 1]] }).cells
  expect(two(9)).not.toBe(two(1)) // a bar sits at its minute: a gap in the readings stays a gap
})

test('the fine layer: braille dots two across and four down a cell, coloured by the brightest; text over them', () => {
  setPalette('default')
  const cv = canvas(2, 1)
  cv.fdot(0, 0, 0xff0000, 0.4) // the cell's top-left dot …
  cv.fdot(0.5, 1.5, 0x00ff00, 1) // … and its bottom-right one, brighter
  const [a, b] = decode(cv.cells())
  expect(a!.ch).toBe(String.fromCharCode(0x2800 + 0x01 + 0x80))
  expect(a!.fg).toBe(0x00ff00)
  expect(b).toEqual({ ch: ' ', fg: C.bg, bg: C.bg })
  cv.write(0, 0, 'x', 0x123456)
  expect(decode(cv.cells())[0]!.ch).toBe('x')
})

test('LIBRARY, SAMPO, STATE, TESTS: a globe over a histogram, a terminal over a table, a tree beside a card, a cell a module', () => {
  setPalette('default')
  const lib = decode(libraryPicture(60, 14, { counts: { cards: 1820, ui: 640, moons: 910, calling: 140 }, hourly: [0, 3, 9, 4] }).cells)
  for (const s of ['cards 1,820', 'moons 910', '−24 h', 'now']) expect([s, textOf(lib).includes(s)]).toEqual([s, true])
  expect(lib.filter(c => c.ch >= '⠁' && c.ch <= '⣿').length).toBeGreaterThan(40) // the globe's dots and lines, fine
  const sampo = textOf(decode(sampoPicture(60, 12, { cmds: [{ cmd: 'sampo next', ok: true }, { cmd: 'sampo check --affected', ok: true }], queued: 12,
    queue: [{ id: 'T-0008', status: 'planned', kind: 'CLEAN/S', title: 'Tidy helpers' }, { id: 'T-0010', status: 'planned', kind: 'FIX/S', title: 'Redact' }] }).cells))
  for (const s of ['$ sampo next', '$ sampo check --affected', 'ID', 'status', 'T-0008', ' planned ', 'Tidy helpers', '+10 more']) expect([s, sampo.includes(s)]).toEqual([s, true])
  const TREE = [{ name: 'tasks', dir: true, n: 412, kb: 0, at: '2026-01-02T09:58:00Z' }, { name: 'ledger.jsonl', dir: false, n: 0, kb: 3310, at: '2026-01-02T09:59:30Z' },
    { name: 'decisions.md', dir: false, n: 0, kb: 84, at: '2026-01-02T08:10:00Z' }]
  const DOC = { kind: 'decision', title: 'Retry once', text: 'One retry covers the slow links.', at: '2026-01-02' }
  const state = textOf(decode(statePicture(60, 12, { tree: TREE, newest: 'ledger.jsonl', doc: DOC }).cells))
  for (const s of ['state/', '▸ tasks/', '412', 'ledger.jsonl', 'decisions.md', '3.2 MB', local('2026-01-02T09:59:30Z'), '2026-01-02', '# Retry once', 'One retry covers'])
    expect([s, state.includes(s)]).toEqual([s, true])
  const tight = textOf(decode(statePicture(43, 15, { tree: TREE, newest: 'ledger.jsonl', doc: DOC }).cells)) // 43 wide: the names whole, the card keeps its file and date
  for (const s of ['ledger.jsonl ', 'decisions.md ', local('2026-01-02T08:10:00Z'), '2026-01-02']) expect([s, tight.includes(s)]).toEqual([s, true])
  const mods = [{ name: 'test_a', state: 'cached' }, { name: 'test_b', state: 'fail' }, { name: 'test_c', state: 'running' }, { name: 'test_d', state: 'idle' }]
  const tests = testsPicture(60, 10, { modules: mods, source: 'last check 09:52' })
  const cells = decode(tests.cells)
  for (const s of ['■ 1 cached', '■ 1 running', '■ 1 fail', '□ 1 idle', '✗ b']) expect([s, textOf(cells).includes(s)]).toEqual([s, true])
  expect([painted(cells, C.err), painted(cells, C.accent)]).toEqual([true, true])
  const lit = (r: number, c: number) => { const x = cells[r * 60 + c]!; return x.fg !== C.bg || x.bg !== C.bg }
  expect([lit(1, 1), lit(4, 58), lit(8, 33)]).toEqual([true, true, true]) // the tiles fill the panel: its left, its right, its foot
  expect(tests.live).not.toBeNull() // a running module pulses
  expect(testsPicture(60, 10, { modules: mods.filter(m => m.state !== 'running'), source: '' }).live).toBeNull() // none running: still
})

const FLOWS = [{ from: 'cost', to: 'qwen3.5:4b', n: 10, hot: 3 }, { from: 'cost --why x', to: 'qwen3.5:9b', n: 5, hot: 0 },
  { from: 'review', to: 'claude', n: 2, hot: 0 }]

test('MODELS: callers on the left, models under their families, a curve a pair as thick as its calls; packets while hot', () => {
  setPalette('default')
  const pic = modelsPicture(70, 16, { links: FLOWS })
  const drawn = decode(pic.cells)
  const text = textOf(drawn)
  for (const s of ['cost', 'review', 'CLAUDE', 'OLLAMA', 'qwen3.5:4b', 'claude']) expect([s, text.includes(s)]).toEqual([s, true])
  expect(text).not.toContain('--why') // a caller is its first words: one `cost` row
  expect([painted(drawn, C.warn), painted(drawn, C.accent2)]).toEqual([true, true]) // claude's orange, ollama's cyan
  expect(pic.live!(1)).not.toBe(pic.live!(1.5)) // calls in the last minute: packets ride
  expect(modelsPicture(70, 16, { links: FLOWS.map(f => ({ ...f, hot: 0 })) }).live).toBeNull() // none: a still picture
  expect(textOf(decode(modelsPicture(70, 16, { links: [] }).cells))).toContain('no model calls')
  const evil = '😀'.repeat(30) + '‮\u0007x\ud800', bad = /[\u0000-\u001f\u007f-\u009f‪-‮⁦-⁩\ud800-\udfff]/
  const hostile = decode(modelsPicture(40, 12, { links: [{ from: evil, to: evil, n: 1, hot: 1 }] }).cells)
  expect(hostile.filter(c => bad.test(c.ch)).length).toBe(0) // a cut never splits an emoji; no control or bidi override
})

test('a picture drawn cold (no memo) stays cheap; printed as FM-PIC (ms)', () => {
  setPalette('default')
  const lanes = ['T-0021', 'T-0022', 'T-0023'].map((id, i) => ({ id, title: 'Live page', color: C.accent2, done: i, total: 4, files: ['a/b.py'], state: 'building', next: false }))
  const mods = Array.from({ length: 300 }, (_, i) => ({ name: `test_${i}`, state: i % 17 ? 'cached' : 'idle' }))
  const cold = (draw: (rows: number) => unknown) => { // a new size each time: never a memo hit
    const ms: number[] = []
    for (let i = 0; i < 5; i++) {
      const t = performance.now()
      draw(12 + i)
      ms.push(performance.now() - t)
    }
    return [...ms].sort((a, b) => a - b)[2]!
  }
  const got = {
    lanes: cold(r => lanesPicture(70, r, lanes)), merge: cold(r => mergePicture(70, r, MERGE)),
    cpu: cold(r => cpuPicture(70, r, CPU)), loop: cold(r => taskPicture(70, r, LOOP)),
    library: cold(r => libraryPicture(70, r, { counts: { cards: 1820, ui: 640, moons: 910, calling: 140 }, hourly: [1, 2, 3] })),
    models: cold(r => modelsPicture(70, r, { links: FLOWS })),
    sampo: cold(r => sampoPicture(70, r, { cmds: [{ cmd: 'sampo next', ok: true }], queued: 1, queue: [{ id: 'T-1', status: 'planned', kind: 'FIX/S', title: 'x' }] })),
    state: cold(r => statePicture(70, r, { tree: [{ name: 'tasks', dir: true, n: 4, kb: 0 }], newest: 'tasks', doc: null })),
    tests: cold(r => testsPicture(70, r, { modules: mods, source: '' })),
  }
  console.log(`FM-PIC cold ${Object.entries(got).map(([k, v]) => `${k}=${v.toFixed(2)}`).join(' ')}`)
  for (const [k, v] of Object.entries(got)) expect([k, v < 50]).toEqual([k, true])
})

function world(on: On, view: unknown, store?: Record<string, unknown>) {
  mock.clock(on)
  mock.store(on, store)
  mock.env(on, { FOREMAN_FM: 'fm', HOME: '/home/u' })
  on('process.run', async ($, e) => ({
    value: { exitCode: 0, stdout: e.argv[1] === 'ui' ? JSON.stringify(view as FmView) : 'ok', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  on('ui.panes', async () => ({ value: [] }))
  on('session.id', async () => ({ value: 'sess-1' }))
  on('ui.open', async () => ({ value: { isPlaced: true } }))
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
const PANE = (bodyColumns: number, bodyRows: number, surface = 'terminal', offset = 0) => ({
  plugin: 'sampo-ui', surface, component: 'Pane', requestId: 'foreman',
  props: { title: 'Sampo', isFocused: true, bodyColumns, placement: 'dock', scroll: { offset, bodyRows }, view: {} },
}) as never
const PANELS = ['lanes', 'merge', 'cpu', 'loop', 'library', 'models', 'sampo', 'state', 'sessions', 'tests']
type Mounted = { findAll: (q: { type: string }) => Promise<{ key?: unknown; props: Record<string, unknown> }[]> }
const pics = async (ui: Mounted) => (await ui.findAll({ type: 'Raster' })).map(r => String(r.key)).filter(k => /^panel-.*-pic$/.test(k))
const width = async (ui: Mounted, k: string) => Number((await ui.findAll({ type: 'Raster' })).find(r => r.key === `panel-${k}-pic`)!.props.columns)

test('the grid never leaves a slot empty: 9, 10 and 11 panels in 1, 2 and 3 columns, every row the full width', () => {
  for (const n of [9, 10, 11])
    for (const cols of [1, 2, 3]) {
      const rows = gridRows(n, cols, 100, 2)
      expect([n, cols, rows.flat().length]).toEqual([n, cols, n])
      for (const [i, r] of rows.entries()) {
        expect([n, cols, i, r.reduce((t, w) => t + w, 0) + (r.length - 1) * 2]).toEqual([n, cols, i, 100]) // full width
        expect([n, cols, i, i < rows.length - 1 ? r.length === cols : r.length >= 1 && r.length <= cols]).toEqual([n, cols, i, true])
      }
    }
  expect(gridRows(10, 3, 100, 2).at(-1)).toEqual([100]) // one left over spans the row
  expect(gridRows(11, 3, 100, 2).at(-1)).toEqual([49, 49]) // two split it
})

test('SESSIONS: every live session orbiting the machine, a context gauge in its project colour, its task and model; idle dim', () => {
  setPalette('default')
  const S = [{ id: 'a1', project: 'demo', color: C.accent2, task: 'T-0007', model: 'opus 5.5', pct: 41, idle: false },
    { id: 'b2', project: 'shop', color: C.edit, task: '', model: 'sonnet 5', pct: 86, idle: true }]
  const drawn = decode(sessionsPicture(64, 14, { sessions: S }).cells)
  const text = textOf(drawn)
  for (const s of [' T-0007 ', 'opus 5.5 41%', ' no task ', 'sonnet 5 86%', 'demo', 'shop']) expect([s, text.includes(s)]).toEqual([s, true])
  expect(drawn.filter(c => c.ch >= '⠁' && c.ch <= '⣿').length).toBeGreaterThan(30) // the orbits and gauges, fine
  expect(drawn[text.indexOf(' no task ')]!.fg).toBe(C.dim) // an idle session dimmed
  expect(textOf(decode(sessionsPicture(64, 14, { sessions: [] }).cells))).toContain('no live sessions')
})

test('the board page: ten Board panels with their chrome, 3, 2 or 1 columns by width, no empty slot', { timeoutMs: 30000 }, async ($, on) => {
  world(on, BUSY)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const wide = await $.ui.mount(PANE(214, 80)) // very wide: three columns, all ten fit, in the Board's order
  expect(await pics(wide)).toEqual(PANELS.map(k => `panel-${k}-pic`))
  expect([await width(wide, 'lanes'), await width(wide, 'tests')]).toEqual([Math.floor((212 - 4) / 3) + 1, 212]) // TESTS alone spans the row
  for (const k of PANELS) expect([k, !!(await wide.find({ key: `panel-${k}-head` })), !!(await wide.find({ key: `panel-${k}-foot` }))]).toEqual([k, true, true])
  expect([!!(await wide.find({ key: 'panel-row-3' })), await wide.find({ key: 'panel-row-4' })]).toEqual([true, undefined])
  await wide.unmount()
  const two = await $.ui.mount(PANE(120, 90)) // two columns: five full rows
  expect([!!(await two.find({ key: 'panel-row-4' })), await two.find({ key: 'panel-row-5' }), (await pics(two)).length]).toEqual([true, undefined, 10])
  expect([await width(two, 'sessions'), await width(two, 'tests')]).toEqual([58, 58])
  await two.unmount()
  const one = await $.ui.mount(PANE(76, 220)) // one column
  expect([!!(await one.find({ key: 'panel-row-9' })), (await pics(one)).length]).toEqual([true, 10])
  await one.press({ key: 'fm-page' }) // p: every section, as before
  expect([!!(await one.find({ key: 'queue-head' })), (await pics(one)).length]).toEqual([true, 0])
  await one.press({ key: 'fm-page' }) // and back
  expect((await pics(one)).length).toBe(10)
  await one.unmount()
})

test('the top bar: SAMPO, the project and the counts; the task as the NOW strip; a callout with key caps when you are needed', async ($, on) => {
  world(on, BUSY)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(PANE(104, 60))
  for (const k of ['head-bar', 'head-now', 'now-title', 'head-callout', 'callout-keys']) expect([k, !!(await ui.find({ key: k }))]).toEqual([k, true])
  for (const t of [/SAMPO/, /demo-1a2b3c/, / queued/, /T-0009 plan awaits your yes/]) expect([String(t), !!(await ui.find({ type: 'Text', text: t }))]).toEqual([String(t), true])
  expect([!!(await ui.find({ key: 'att-approve-T-0009' })), !!(await ui.find({ key: 'review-T-0009' }))]).toEqual([true, true]) // [a] approve, [s] show
  for (const k of ['fm-page', 'next', 'drive']) expect([k, !!(await ui.find({ key: k }))]).toEqual([k, true]) // the footer's key caps
  await ui.unmount()
})

test('the top bar with nothing needing you: the NOW strip, no callout', async ($, on) => {
  world(on, { ...TASK, queue: (TASK.queue as { waits?: string | null }[]).filter(q => !q.waits), checks: null, signals: [], approvals: [] })
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const quiet = await $.ui.mount(PANE(104, 60))
  expect([await quiet.find({ key: 'head-callout' }), !!(await quiet.find({ key: 'head-now' }))]).toEqual([undefined, true]) // nothing needs you: no callout
  await quiet.unmount()
})

test('a dock too short for all ten: the panels the state makes relevant first, only those in the window drawn', async ($, on) => {
  world(on, BUSY)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  // lanes building and waiting, a train, a task, models called: LANES, MERGE, LOOP, MODELS lead; the rest after
  const top = await $.ui.mount(PANE(76, 40))
  expect(await pics(top)).toEqual(['panel-lanes-pic', 'panel-merge-pic', 'panel-loop-pic'])
  expect(await top.find({ key: 'panel-tests-head' })).toBeDefined() // off screen: its chrome, no picture drawn
  await top.unmount()
  const down = await $.ui.mount(PANE(76, 40, 'terminal', 40)) // scrolled a screen down
  expect(await pics(down)).toEqual(['panel-loop-pic', 'panel-models-pic', 'panel-cpu-pic', 'panel-library-pic'])
  await down.unmount()
})

test('off the terminal each panel is its words; a session starts on the page the store kept', async ($, on) => {
  world(on, TASK, { page: 'details' })
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const kept = await $.ui.mount(PANE(76, 60))
  expect([!!(await kept.find({ key: 'queue-head' })), (await pics(kept)).length]).toEqual([true, 0])
  await kept.press({ key: 'fm-page' })
  await kept.unmount()
  const desk = await $.ui.mount(PANE(120, 90, 'desktop'))
  expect((await desk.findAll({ type: 'Raster' })).filter(r => /^panel-/.test(String(r.key))).length).toBe(0)
  expect(await desk.find({ type: 'Text', text: /T-0007 · 1\/2 criteria · 2\/3 steps/ })).toBeDefined()
  await desk.unmount()
})
