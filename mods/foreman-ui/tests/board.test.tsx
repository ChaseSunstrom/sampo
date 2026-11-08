import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { BOARD } from '../hooks/board-palette'
import { C, PALETTES, checkCells, dotCanvas, flowCells, histCells, laneClusters, layout, panelAccent, setPalette } from '../hooks/kit'
import type { FmView } from '../types'
import BUSY from './fixtures/board-busy'

// T-3701: the mod in the Board's look (the live page's Board, T-3659): its palette by default, every pane section a
// Board panel drawing its mechanism, the band a line of pills, the chat rows a chip per kind.

test('the default palette is the Board kit\'s, from board-tokens.json; forge keeps the old one', () => {
  setPalette('default')
  expect([C.bg, C.line, C.dim, C.muted, C.on]).toEqual([BOARD.bg, BOARD.line, BOARD.dim, BOARD.muted, BOARD.on])
  expect([C.accent, C.accent2, C.ok, C.err, C.agent, C.warn]).toEqual([BOARD.yellow, BOARD.cyan, BOARD.green, BOARD.red, BOARD.violet, BOARD.orange])
  expect([PALETTES.default.accent, PALETTES.default.ok]).toEqual([BOARD.yellow, BOARD.green])
  setPalette('forge')
  expect([C.accent, C.accent2, C.ok, C.err, C.hot, C.steel]).toEqual([0xffc24d, 0x6e9bff, 0x6fd8a0, 0xff85bc, 0xff6a4a, 0xc8d8ec])
  setPalette('default')
})

function world(on: On, view: FmView = BUSY as unknown as FmView, page?: string) {
  mock.clock(on)
  mock.store(on, page ? { page } : undefined)
  mock.env(on, { FOREMAN_FM: 'fm', HOME: '/home/u' })
  const opened: Record<string, unknown>[] = []
  on('process.run', async ($, e) => ({
    value: { exitCode: 0, stdout: e.argv[1] === 'ui' ? JSON.stringify(view) : 'ok', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  on('ui.panes', async () => ({ value: [] }))
  on('session.id', async () => ({ value: 'sess-1' }))
  on('ui.open', async ($, e) => {
    opened.push(e as unknown as Record<string, unknown>)
    return { value: { isPlaced: true } }
  })
  on('ui.toast', async () => ({ value: undefined }))
  on('ui.blit', async () => ({ value: undefined }))
  on('session.usage', async () => ({ value: { startedAt: 0, context: { window: 1000, percent: 30 }, rateLimits: [], cost: { usd: 1.5 } } }))
  on('agent.list', async () => ({ value: [] }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  on('tool.call', async () => ({ result: {} }))
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('ui.render', async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  return { opened }
}

const decode = (b64: string) => {
  const bin = atob(b64)
  const view = new DataView(new Uint8Array([...bin].map(c => c.charCodeAt(0))).buffer)
  const out: { ch: string; fg: number; bg: number }[] = []
  for (let i = 0; i < bin.length; i += 12)
    out.push({ ch: String.fromCodePoint(view.getUint32(i, true)), fg: view.getUint32(i + 4, true), bg: view.getUint32(i + 8, true) })
  return out
}

const PANE = (bodyColumns: number, bodyRows: number, surface = 'terminal') => ({
  plugin: 'sampo-ui', surface, component: 'Pane', requestId: 'foreman',
  props: { title: 'Sampo', isFocused: true, bodyColumns, placement: 'dock', scroll: { offset: 0, bodyRows }, view: {} },
}) as never
const KEYS = ['task', 'lanes', 'merge', 'activity', 'agents', 'queue', 'cpu', 'checks', 'sessions', 'history', 'today']

// the drawn tree as nodes, and a node's text (Text keys aren't kept in it: the panel's parts sit in keyed Boxes)
type N = { type?: string; props?: Record<string, unknown>; children?: unknown[] }
const nodes = (t: unknown): N[] => (!t || typeof t !== 'object' ? [] : Array.isArray(t) ? t.flatMap(nodes) : [t as N, ...nodes((t as N).children ?? [])])
const textOf = (n: unknown): string =>
  typeof n === 'string' ? n : typeof n === 'number' ? String(n) : !n || typeof n !== 'object' ? '' : Array.isArray(n) ? n.map(textOf).join('') : textOf((n as N).children ?? [])

test('every pane section is a Board panel: a numbered chip, its UPPERCASE name, an accent label, corner brackets, a footer', async ($, on) => {
  world(on, undefined, 'details') // the second page: every section
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const idle = await $.ui.mount(PANE(90, 90))
  expect(nodes(await idle.drawn()).some(n => n.props?.key === 'activity-head')).toBe(false) // no call yet: no tools panel
  await idle.unmount()
  await $.tool.call({ tool: 'Bash', command: 'ls', description: 'list' } as never)
  const ui = await $.ui.mount(PANE(90, 90))
  const all = nodes(await ui.drawn())
  const byKey = (k: string) => all.find(n => n.props?.key === k)
  const drawn = KEYS.filter(k => byKey(`${k}-head`))
  expect(drawn).toEqual(KEYS) // a tall dock and a busy view: every panel
  for (const [i, key] of drawn.entries()) {
    expect([key, textOf(byKey(`${key}-chip`))]).toEqual([key, ` ${String(i + 1).padStart(2, '0')} `]) // numbered in the order drawn
    expect(nodes(byKey(`${key}-chip`)).some(n => n.type === 'Text' && n.props?.backgroundColor)).toBe(true)
    const word = textOf(byKey(`${key}-name`))
    expect([key, word.length > 0, word]).toEqual([key, true, word.toUpperCase()])
    expect([key, textOf(byKey(`${key}-label`)).length > 0]).toEqual([key, true])
    const head = textOf(byKey(`${key}-head`))
    expect([key, head.startsWith('┌ '), head.endsWith(' ┐')]).toEqual([key, true, true])
    const foot = textOf(byKey(`${key}-foot`))
    expect([key, foot.startsWith('└ '), foot.endsWith(' ┘')]).toEqual([key, true, true])
  }
  await ui.unmount()
})

test('each panel draws its mechanism: lane clusters, merge flows, the CPU stack and goal, gate cells, the history area', async ($, on) => {
  world(on, undefined, 'details')
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(PANE(90, 90))
  const rasters = (await ui.findAll({ type: 'Raster' })).map(r => String(r.key))
  expect(rasters).toEqual(expect.arrayContaining(['lanes-graph', 'merge-flow', 'cpu-hist', 'checks-cells', 'history-spark']))
  const cpu = decode(String((await ui.find({ type: 'Raster', key: 'cpu-hist' }))!.props.cells))
  expect(cpu.some(c => c.ch === '╌')).toBe(true) // the goal line
  const texts = nodes(await ui.drawn()).filter(n => n.type === 'Text').map(textOf)
  expect(['6.4%', '✗ bench · slower than usual', ' T-0021 ', ' ▣ build ', '✓ T-0019'].filter(w => !texts.includes(w))).toEqual([])
  await ui.unmount()
  const desk = await $.ui.mount(PANE(90, 90, 'desktop')) // no Raster there: each draws its text twin
  expect((await desk.findAll({ type: 'Raster' })).length).toBe(0)
  const twins = nodes(await desk.drawn())
  for (const key of ['lanes-graph', 'merge-flow', 'cpu-hist', 'checks-cells', 'history-spark'])
    expect([key, textOf(twins.find(n => n.props?.key === key)).trim().length > 0]).toEqual([key, true])
  await desk.unmount()
})

test('the queue is a table: a header, then each row its id, a status pill and its title', async ($, on) => {
  world(on, undefined, 'details')
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(PANE(90, 90))
  const texts = nodes(await ui.drawn()).filter(n => n.type === 'Text')
  expect(texts.some(n => textOf(n) === 'STATUS')).toBe(true)
  for (const [word, color] of [['building', C.accent2], ['plan    ', C.warn], ['yours   ', C.accent]] as const) {
    const p = texts.find(n => textOf(n) === ` ${word} `)
    expect([word, p?.props?.color]).toEqual([word, `#${color.toString(16).padStart(6, '0')}`])
    expect(p?.props?.backgroundColor).toBeDefined()
  }
  await ui.unmount()
})

test('the pane paints the Board ground under its body in the Board palette, and not in the others', async ($, on) => {
  const { opened } = world(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'fm', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 200 } })
  expect(opened.some(o => o.backgroundColor === `#${BOARD.bg.toString(16).padStart(6, '0')}`)).toBe(true)
})

const BAND = (bodyColumns: number) => ({
  plugin: 'sampo-ui', surface: 'terminal', component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 14, bodyColumns, scroll: { offset: 0, bodyRows: 14 }, view: {} },
}) as never

test('the band is one dense line of the Board: a chip, the counts as pills, today\'s meter, what is next', async ($, on) => {
  world(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(BAND(140))
  const texts = nodes(await ui.drawn()).filter(n => n.type === 'Text')
  const lit = (s: string) => texts.find(n => textOf(n) === s)?.props?.backgroundColor
  for (const s of [' SAMPO ', ' ✓ 4 done ', ' 2 building ', ' 2 to merge ', ' 1 to close ', ' 12 queued ', ' 5 inbox ']) expect([s, !!lit(s)]).toEqual([s, true])
  expect(texts.some(n => textOf(n) === 'next T-0010 Redact tokens in the log' || /^next T-00/.test(textOf(n)))).toBe(true)
  expect((await ui.findAll({ type: 'Raster' })).some(r => r.key === 'fm-band-queue')).toBe(true)
  await ui.unmount()
})

const tool = (tool: string, input: object, running: boolean) => ({
  plugin: 'sampo-ui', surface: 'terminal', component: 'ToolUse', requestId: `tu-${tool}`,
  props: { tool_use_id: `tu-${tool}`, tool, input, isRunning: running, isErrored: false, isInterrupted: false, output: 'ok' },
}) as never

test('a tool row carries a chip naming its kind on its accent; the label keeps its words', async ($, on) => {
  world(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'go', turnId: 't1' })
  for (const [name, input, running, word, color] of [
    ['Bash', { command: 'make', description: 'build it' }, true, ' RUN ', C.accent],
    ['Edit', { file_path: '/repo/a.py', old_string: 'a', new_string: 'b' }, true, ' EDIT ', C.edit],
    ['Read', { file_path: '/repo/a.py' }, false, ' READ ', C.accent2],
  ] as const) {
    const ui = await $.ui.mount(tool(name, input, running))
    const chipText = nodes(await ui.drawn()).find(n => n.type === 'Text' && textOf(n) === word)
    expect([name, chipText?.props?.backgroundColor]).toEqual([name, `#${color.toString(16).padStart(6, '0')}`])
    await ui.unmount()
  }
})

test('the other palettes keep coloured words: no chip block behind a tool row or a pill', { options: { look: 'forge' } }, async ($, on) => {
  world(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'go', turnId: 't1' })
  const row = await $.ui.mount(tool('Bash', { command: 'make' }, true))
  expect(JSON.stringify(await row.drawn())).not.toContain('backgroundColor')
  await row.unmount()
  const band = await $.ui.mount(BAND(140))
  expect(JSON.stringify(await band.drawn())).not.toContain('backgroundColor')
  await band.unmount()
  setPalette('default')
})

test('kit: a whole panel (a chart) gets all its rows or none, never squashed; its rows go to the others', () => {
  const parts = (whole: boolean) => [
    { key: 'task', want: 10, cap: 10, min: 4, spare: true },
    { key: 'queue', want: 6, cap: 6 },
    { key: 'today', want: 6, cap: 6, min: 6, whole },
  ]
  expect(layout(18, parts(false)).rows.today).toBeLessThan(6) // squashed
  const L = layout(18, parts(true))
  expect(L.rows.today).toBe(0)
  expect(L.rows.task! + L.rows.queue!).toBe(18 - 4 - 2) // head and controls, two titles: every other row placed
  expect(layout(40, parts(true)).rows.today).toBeGreaterThanOrEqual(6)
})

test('kit: the dot canvas sets braille dots, 2x4 a cell, in the colour drawn last', () => {
  const cv = dotCanvas(2, 1)
  cv.set(0, 0, 0x111111)
  cv.set(3, 3, 0x222222)
  cv.set(9, 9, 0x333333) // off the canvas: nothing
  expect(decode(cv.cells())).toEqual([{ ch: '⠁', fg: 0x111111, bg: 0x01000000 }, { ch: '⢀', fg: 0x222222, bg: 0x01000000 }])
  const dashed = dotCanvas(8, 1)
  dashed.line(0, 0, 15, 0, 0x444444, 1)
  expect(decode(dashed.cells()).map(c => c.ch).join('')).toBe('⠁'.repeat(8)) // every other dot: the left of each cell
})

test('kit: lane clusters draw a hub per lane in its colour, its done steps lit, links between hubs', () => {
  setPalette('default')
  const lanes = [{ id: 'T-1', color: 0xff0000, done: 2, total: 4 }, { id: 'T-2', color: 0x00ff00, done: 0, total: 3 }]
  const drawn = decode(laneClusters(lanes, 28, 3))
  expect(drawn.length).toBe(28 * 3)
  const colors = new Set(drawn.filter(c => c.ch !== ' ').map(c => c.fg))
  expect(colors.has(0xff0000) && colors.has(0x00ff00)).toBe(true)
  const middle = drawn.slice(28, 56) // the hubs' row: the left cluster red, the right green, a link between
  expect(middle.findIndex(c => c.fg === 0xff0000)).toBeLessThan(middle.findIndex(c => c.fg === 0x00ff00))
  expect(drawn.every(c => ' ●○◉'.includes(c.ch) || (c.ch.codePointAt(0)! >= 0x2800 && c.ch.codePointAt(0)! <= 0x28ff))).toBe(true)
  expect([drawn.filter(c => c.ch === '◉').length, drawn.filter(c => c.ch === '●' && c.fg === 0xff0000).length]).toEqual([2, 2]) // hubs; T-1's 2 done
})

test('kit: flows leave each source row on the left and arrive at the target row on the right', () => {
  const drawn = decode(flowCells([0xff0000, 0x0000ff], 1, 12, 3))
  const at = (row: number, col: number) => drawn[row * 12 + col]!
  expect(at(0, 0).fg).toBe(0xff0000) // the first source starts on row 0
  expect(at(1, 0).fg).toBe(0x0000ff) // the second on row 1
  expect([at(1, 11).ch !== ' ', at(0, 11).ch, at(2, 11).ch]).toEqual([true, ' ', ' ']) // both end on the target row
})

test('kit: stacked columns put each series in its colour from the bottom, the goal dashed across', () => {
  const drawn = decode(histCells([{ color: 0xaa0000, values: [8, 8] }, { color: 0x00aa00, values: [8, 0] }], 3, 2, 16, 4, 1))
  const at = (row: number, col: number) => drawn[row * 3 + col]! // row 0 is the top
  expect([at(0, 0).ch, at(0, 0).fg]).toEqual(['█', 0x00aa00]) // the second series on top of the first
  expect([at(1, 0).ch, at(1, 0).bg]).toEqual(['╌', 0xaa0000]) // the goal (bottom row) dashed over the bar
  expect(at(0, 1).ch).toBe(' ') // the gap column
  expect([at(0, 2).ch, at(1, 2).ch]).toEqual([' ', '╌']) // 8 of 16: the bottom row only, the goal over it
  const plain = decode(histCells([{ color: 0x123456, values: [1, 2, 4] }], 3, 1, 4, null, 0))
  expect(plain.map(c => c.ch).join('')).toBe('▂▄█')
})

test('kit: a cell per gate, failing ones lit, wrapped to the width', () => {
  setPalette('default')
  const g = checkCells([{ exit: 0 }, { exit: 1 }, { exit: 0 }, { exit: 0 }], 8)
  expect([g.columns, g.rows]).toEqual([8, 2])
  const drawn = decode(g.cells)
  expect(drawn.slice(3, 5).every(c => c.fg === C.err)).toBe(true)
  expect(drawn[0]!.fg).not.toBe(C.err)
  expect([panelAccent(1), panelAccent(6)]).toEqual([C.accent2, C.accent2])
})
