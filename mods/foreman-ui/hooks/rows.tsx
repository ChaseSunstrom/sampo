import type { EngineInterface, RenderElement, ResolveInput } from 'claude-code'
import type { LiveAgent } from '../types'
import { C, MASCOT_COLORS, cells, crest, crestText, TONE_COLOR, activityCells, agentColor, barCells, changedLines, clawd, clean, diffRows, elapsed, ganttCells, hex, spin, miniClawd, mix, outputSummary, progressCells, ruleCells, shortPath, sparkCells, sparkText, stageCells, stageLabels, textBar, textComet, toolFace } from './kit'
import type { DiffRow, LabelRun } from './kit'
import { OUT_LINES, TINY_OUT, TREE_MS, cfg, diffCap, took } from './state'
import type { FeedItem, Said, ToolUseRender } from './state'

// T-0130: the transcript's Sampo rows and the small drawings the band and pane share; pure but for the
// atoms they read and the shared timing maps. They take the resolved element table, not $: the engine follows
// $ only into functions declared in the file that uses it.
export type UI = ReturnType<EngineInterface['ui']['resolve']>

/** A bar: a true-color Raster on the terminal, its text twin elsewhere. `fraction` null draws an activity comet. */
export function meter(
  ui: UI,
  e: ResolveInput,
  key: string,
  width: number,
  fraction: number | null,
  color: number,
  f: number | null,
) {
  if (e.surface === 'terminal') {
    const { Raster } = ui
    const cells =
      fraction === null ? activityCells(width, color, f ?? 0) : progressCells(width, fraction, mix(color, C.accent2, 0.6), color, f)
    return <Raster key={key} columns={width} rows={1} cells={cells} />
  }
  const { Box, Text } = ui
  if (fraction === null) {
    const [a, b, c] = textComet(width, f ?? 0)
    return (
      <Box key={key} flexDirection="row">
        <Text color={hex(C.track)}>{a}</Text>
        <Text color={hex(color)}>{b}</Text>
        <Text color={hex(C.track)}>{c}</Text>
      </Box>
    )
  }
  const [done, rest] = textBar(width, fraction)
  return (
    <Box key={key} flexDirection="row">
      <Text color={hex(color)}>{done}</Text>
      <Text color={hex(C.track)}>{rest}</Text>
    </Box>
  )
}

export type Part = { text: string; color?: number; bold?: boolean }
export const LIGHT = 0xd0d5dd // body text that reads, as in the pane

/** T-1031: a chat row's head, as the pane's section titles ('make the full chat UI match the panel look'): a mark, a
 * bold label in the row's colour, its subject, a rule fading out in that colour (a comet runs along it while `frame`),
 * the facts muted on the right. A subject too long to leave room for the rule comes back as `rest`, the body's first
 * line, so nothing is cut. */
export function sectionHead(ui: UI, e: ResolveInput, o: { key: string; color: number; label: string; mark?: string;
  markColor?: number; markKey?: string; subject?: string; meta?: Part[]; frame?: number | null; width?: number }): { head: RenderElement; rest: string } {
  const { Box, Raster, Text } = ui
  const width = Math.max(30, (o.width ?? (e.viewport?.columns ?? 100) - (treeOn(e) ? TREE_INDENT : 0)) - 2) // T-1040: beside the gutter
  const metaLen = (o.meta ?? []).reduce((n, p) => n + p.text.length, 0)
  const room = width - (o.mark ? 2 : 0) - o.label.length - 1 - (metaLen ? metaLen + 1 : 0)
  const fits = !!o.subject && o.subject.length <= room - 8
  const rw = Math.max(3, room - (fits ? o.subject!.length + 1 : 0))
  const rule = e.surface === 'terminal'
    ? <Raster key={`${o.key}-rule`} columns={rw} rows={1} cells={o.frame == null ? ruleCells(rw, o.color) : activityCells(rw, o.color, o.frame)} />
    : <Text key={`${o.key}-rule`} color={hex(mix(C.track, o.color, 0.5))}>{'─'.repeat(rw)}</Text>
  const head = (
    <Box key={`${o.key}-head`} flexDirection="row" gap={1} height={1} overflow="hidden">
      {o.mark ? <Box key={o.markKey ?? `${o.key}-mark`} flexShrink={0}><Text weight="bold" color={hex(o.markColor ?? o.color)}>{o.mark}</Text></Box> : null}
      <Box flexShrink={0}><Text weight="bold" color={hex(o.color)}>{o.label}</Text></Box>
      {fits ? <Box flexShrink={0}><Text color={hex(LIGHT)}>{o.subject}</Text></Box> : null}
      <Box flexShrink={0}>{rule}</Box>
      {metaLen ? (
        <Box flexShrink={0}>
          <Text>{(o.meta ?? []).map((p, i) => <Text key={i} weight={p.bold ? 'bold' : 'regular'} color={hex(p.color ?? C.muted)}>{p.text}</Text>)}</Text>
        </Box>
      ) : null}
    </Box>
  )
  return { head, rest: !o.subject || fits ? '' : o.subject }
}

// T-1040: the chat as a tree. The engine draws each row on its own and owns their order, so every row of a turn draws its
// own piece: the prompt is the root with the rail starting under it; a row hangs on ├─ with the rail beside the rest of
// it; a later block of the same row (a reply's next part, the engine's result block) is rail only; the turn's closing
// line is └─. The rail is a column of │ placed over the gutter (position absolute, top to bottom), so it is exactly as
// tall as the row however its text wraps. No width on a Box around the row: the engine refuses its own rows under one.
export const TREE_INDENT = 3
export const TREE_LINE = mix(C.track, C.muted, 0.35) // the band tree's line colour
const RAIL = Array.from({ length: 400 }, () => '│').join('\n') // ponytail: a row taller than 400 lines loses its rail below that

export const treeOn = (e: { surface?: string }) => cfg.chat !== 'full' && e.surface === 'terminal' // elsewhere the surface lays rows out

export function railBox(ui: UI, key: string, top: number) {
  const { Box, Text } = ui
  return (
    <Box key={key} position="absolute" left={0} top={top} bottom={0} width={1} overflow="hidden">
      <Text color={hex(TREE_LINE)}>{RAIL}</Text>
    </Box>
  )
}

/** One row of a turn's tree. `at`: head (├─, the default), body (rail only: a later part of the row above), tip (└─,
 * the turn's last line). `gap`: a rail line above it in place of the blank line that parts sections. The chat setting
 * full: the row as it is. */
export function branch(ui: UI, e: ResolveInput, key: string, row: RenderElement, o: { at?: 'head' | 'body' | 'tip'; gap?: boolean } = {}): RenderElement {
  if (!treeOn(e)) return row
  const { Box, Text } = ui
  const at = o.at ?? 'head'
  return (
    <Box key={key} flexDirection="column">
      {o.gap ? <Text key={`${key}-gap`} color={hex(TREE_LINE)}>│</Text> : null}
      <Box key={`${key}-row`} flexDirection="row">
        <Box flexShrink={0} width={TREE_INDENT}>{at === 'body' ? null : <Text color={hex(TREE_LINE)}>{at === 'tip' ? '└─ ' : '├─ '}</Text>}</Box>
        <Box flexDirection="column" flexGrow={1} flexShrink={1}>{row}</Box>
        {at === 'tip' ? null : railBox(ui, 'fm-rail', at === 'body' ? 0 : 1)}
      </Box>
    </Box>
  )
}

/** The space above a section row: none in the tree (branch draws a rail line there), a blank line in full */
export const gapTop = (e: { surface?: string }) => (treeOn(e) ? 0 : 1)

/** T-1031: a body line under a section head: a quiet guide, then the text */
export function bodyLine(ui: UI, key: string, text: string, color: number = C.muted, guide = '│') {
  const { Text } = ui
  return (
    <Text key={key} wrap="truncate-end">
      <Text color={hex(C.track)}>{`  ${guide} `}</Text>
      <Text color={hex(color)}>{text}</Text>
    </Text>
  )
}

/** A sparkline: a Raster on the terminal, block characters elsewhere. */
export function spark(ui: UI, e: ResolveInput, key: string, values: readonly number[], width: number) {
  if (e.surface === 'terminal') {
    const { Raster } = ui
    return <Raster key={key} columns={width} rows={1} cells={sparkCells(values, width)} />
  }
  const { Text } = ui
  return <Text color={hex(C.accent2)}>{sparkText(values, width)}</Text>
}

// T-0909: the pane's charts. On the terminal a Raster (and, while `motion` is given, a closure that redraws it for the
// 30 fps blit); elsewhere their text twins.
export type Motion = Map<string, { columns: number; rows: number; draw: (f: number) => string }>

/** The stage rail with each stage's short name under its node. */
export function rail(ui: UI, e: ResolveInput, stages: readonly string[], current: number, width: number, f: number | null, motion?: Motion) {
  const { Box, Text } = ui
  if (e.surface !== 'terminal')
    return (
      <Text key="stage-text" wrap="wrap">
        {stages.map((s, i) => `${i < current ? '●' : i === current ? '◉' : '○'} ${s}`).join('  ')}
      </Text>
    )
  const { Raster } = ui
  if (f !== null) motion?.set('stage-rail', { columns: width, rows: 1, draw: t => stageCells(stages.length, current, width, t) })
  const color: Record<LabelRun['kind'], number> = { gap: C.dim, done: mix(C.steel, C.dim, 0.35), current: C.hot, todo: C.dim } // T-2876: heat
  return (
    <Box key="stage" flexDirection="column">
      <Raster key="stage-rail" columns={width} rows={1} cells={stageCells(stages.length, current, width, f)} />
      <Text key="stage-labels" wrap="truncate-end">
        {stageLabels(stages, current, width).map((r, i) => (
          <Text key={`sl-${i}`} color={hex(color[r.kind])} weight={r.kind === 'current' ? 'bold' : 'regular'}>
            {r.text}
          </Text>
        ))}
      </Text>
    </Box>
  )
}

/** The band's rail in small: a node every other column, no labels; nothing off the terminal (the band names the stage). */
export function miniRail(ui: UI, e: ResolveInput, n: number, current: number, f: number | null) {
  if (e.surface !== 'terminal' || current < 0) return null
  const { Raster } = ui
  return <Raster key="fm-band-rail" columns={n * 2 - 1} rows={1} cells={stageCells(n, current, n * 2 - 1, f)} />
}

/** Column bars, `rows` tall (Raster), or a sparkline elsewhere. */
export function bars(ui: UI, e: ResolveInput, key: string, values: readonly number[], rows: number, from: number, to: number, bw = 1) {
  if (e.surface !== 'terminal') {
    const { Text } = ui
    return <Text key={key} color={hex(to)}>{sparkText(values, values.length)}</Text>
  }
  const { Raster } = ui
  return <Raster key={key} columns={values.length * (bw + 1) - 1} rows={rows} cells={barCells(values, rows, from, to, bw)} />
}

/** The mascot: Claude Code's own Claude, dancing while work runs, with a mini Claude per running subagent. */
export function mascotTree(ui: UI, e: ResolveInput, state: 'work' | 'idle' | 'happy', n: number, subs: LiveAgent[]) {
  if (cfg.mascot === 'off' || cfg.mascot === 'sampo') return null
  const color = MASCOT_COLORS[cfg.mascot] ?? MASCOT_COLORS.blue!
  const { Box, Text } = ui
  if (e.surface !== 'terminal')
    return <Text color={hex(color)}>{state === 'work' ? (n % 2 ? '(•̀ᴗ•́)و' : '(•̀ᴗ•́)ง') : state === 'happy' ? '\\(^ᴗ^)/' : '(•ᴗ•)'}</Text>
  const running = subs.filter(s => !s.done)
  return (
    <Box key="fm-mascot" flexDirection="row" alignItems="flex-end" gap={1} flexShrink={0}>
      {running.length > 4 && <Text color={hex(C.dim)}>+{running.length - 4}</Text>}
      {running.slice(0, 4).map((s, i) => (
        <Box key={`mini-${s.id}`} flexDirection="column">
          {miniClawd(Math.floor(n / 4) + i).map((row, j) => (
            <Text key={`mini-${s.id}-${j}`} color={hex(agentColor(s.id))}>
              {row}
            </Text>
          ))}
        </Box>
      ))}
      <Box flexDirection="column">
        {clawd(state, n).map((row, j) => (
          <Text key={`clawd-${j}`} color={hex(color)}>
            {row}
          </Text>
        ))}
      </Box>
    </Box>
  )
}

/** T-3113: the crest across the pane's top (mascot 'sampo'): its rows (0 when not drawn) and the element. A terminal
 * gets the Raster, any other surface one line of text. */
export function crestTree(ui: UI, e: ResolveInput, width: number, height: number, state: 'work' | 'idle' | 'happy', n: number, subs: LiveAgent[]) {
  if (cfg.mascot !== 'sampo' || height < 28) return { rows: 0, el: null } // a short dock keeps every row for the work
  const running = subs.filter(s => !s.done)
  const { Raster, Text } = ui
  if (e.surface !== 'terminal')
    return { rows: 1, el: <Text key="fm-crest" color={hex(C.accent)} wrap="truncate-end">{crestText(state, running.length, n)}</Text> }
  const g = crest(width, state, n, running.map(s => s.id), cfg.motion !== 'full', height)
  return { rows: g.rows, el: <Raster key="fm-crest" columns={g.cols} rows={g.rows} cells={cells(g.grid)} /> }
}

/** T-1031: a diff as Sampo text: a line of context dim, a ┄ break between hunks, the line number dim, the +/- sign
 * in ok/err, the text in the softer add/del colours (T-0143), and in a changed line only the words that differ washed
 * in the sign's colour, never the whole width ('the green/red are weird in contrast'). */
export function diffLines(ui: UI, e: ResolveInput, rows: readonly DiffRow[], key: string) {
  const { Text } = ui
  const width = String(Math.max(0, ...rows.map(r => ('n' in r ? r.n : 0)))).length
  const BG = 0x181a21
  return rows.map((r, i) => {
    if (r.kind === 'hunk') return <Text key={`${key}-${i}`} color={hex(C.track)}>{`  ┆ ${'┄'.repeat(width)}`}</Text>
    const gutter = [
      <Text key="g" color={hex(C.track)}>{'  │ '}</Text>,
      <Text key="n" color={hex(C.dim)}>{`${String(r.n).padStart(width)} `}</Text>,
    ]
    if (r.kind === 'ctx')
      return <Text key={`${key}-${i}`} wrap="truncate-end">{gutter}<Text color={hex(C.dim)}>{'  '}</Text><Text color={hex(C.dim)}>{r.text}</Text></Text>
    const add = r.kind === '+'
    const tone = add ? C.add : C.del
    const [p, q] = r.mid ?? [0, 0]
    return (
      <Text key={`${key}-${i}`} wrap="truncate-end">
        {gutter}
        <Text color={hex(add ? C.ok : C.err)}>{`${r.kind} `}</Text>
        {r.mid
          ? [
              ...(p ? [<Text key="a" color={hex(tone)}>{r.text.slice(0, p)}</Text>] : []),
              <Text key="b" color={hex(mix(tone, C.white, 0.35))} backgroundColor={hex(mix(add ? C.ok : C.err, BG, 0.7))}>{r.text.slice(p, q)}</Text>,
              ...(q < r.text.length ? [<Text key="c" color={hex(tone)}>{r.text.slice(q)}</Text>] : []),
            ]
          : <Text color={hex(tone)}>{r.text}</Text>}
      </Text>
    )
  })
}

/** The first `max` changed lines of a diff's rows, with the context and breaks around them, and how many changed
 * lines were left out. */
export function takeChanges(rows: readonly DiffRow[], max: number): { rows: DiffRow[]; left: number } {
  const changes = rows.filter(r => r.kind === '+' || r.kind === '-').length
  if (max <= 0) return { rows: [], left: changes } // no context line without its change (review)
  let n = 0
  let end = rows.length
  for (let i = 0; i < rows.length; i++) {
    if (rows[i]!.kind !== '+' && rows[i]!.kind !== '-') continue
    if (n === max) { end = i; break }
    n++
  }
  const out = rows.slice(0, end)
  while (out.length && out.at(-1)!.kind === 'hunk') out.pop()
  return { rows: out, left: changes - n }
}

export const more = (n: number, what: string) => `… ${n} more ${what}${n === 1 ? '' : 's'}`

/** T-0137/T-0143: a finished shell command as a Sampo row that reads like the running one: its mark, `❯ Ran` and the
 * description (or the command's first line), time and lines; the command under it, dim, whole up to three lines and
 * never silently cut (a chained or multi-line command must not read as a plain one: the T-0095 review's HIGH
 * finding); then the output (failures first when it failed, else the tail), the files it changed, a commit, a
 * timeout. The engine's result block under it is blanked (ToolResult), so nothing draws twice. */
export function shellRow(ui: UI, e: ToolUseRender) {
  const { Box, Text } = ui
  type Out = {
    stdout?: unknown
    stderr?: unknown
    interrupted?: boolean
    backgroundTaskId?: unknown
    timedOutAfterMs?: unknown
    gitOperation?: { commit?: { sha?: unknown; branch?: unknown } }
    bashEditDiff?: { hunks?: unknown; skippedLarge?: unknown; restricted?: unknown } // Claude Code's own, untyped
  }
  const o = (e.props.output && typeof e.props.output === 'object' ? e.props.output : {}) as Out
  const input = (e.props.input ?? {}) as { command?: unknown; description?: unknown }
  const lines = String(input.command ?? '').split('\n').map(clean)
  const desc = typeof input.description === 'string' ? clean(input.description).trim() : ''
  // T-1040: in the tree one line of the command, cut at the edge, and a count of the rest: what ran stays in view (a
  // description must not stand in for a chained command: the T-0095 review), in a line, not a block
  const cap = treeOn(e) ? 1 : 3
  const shown = desc ? lines.slice(0, cap) : lines.slice(1, treeOn(e) ? 2 : 3) // without a description the first line is the title
  const unseen = lines.length - (desc ? 0 : 1) - shown.length
  const bad = !!e.props.isErrored // the exit status decides, not words in the output
  const all = typeof e.props.output === 'string' ? outputSummary(e.props.output, '', OUT_LINES, bad) : outputSummary(o.stdout, o.stderr, OUT_LINES, bad)
  const s = treeOn(e) && !bad && all.total > TINY_OUT ? { ...all, lines: [], more: 0 } : all // T-1040: a pass folds its output
  const ms = took.get(e.props.tool_use_id)
  const bg = typeof o.backgroundTaskId === 'string'
  const stopped = e.props.isInterrupted || o.interrupted
  const mark = stopped ? '■' : bg ? '◷' : bad ? '✗' : '✓'
  const meta = [ms !== undefined ? elapsed(ms) : '', bg ? 'in the background' : s.total ? `${s.total} line${s.total === 1 ? '' : 's'}` : ''].filter(Boolean)
  const files = Array.isArray(o.bashEditDiff?.hunks) ? (o.bashEditDiff.hunks as { path?: unknown; hunks?: unknown }[]) : []
  const unshown = [o.bashEditDiff?.skippedLarge, o.bashEditDiff?.restricted].reduce<number>((n, x) => n + (Array.isArray(x) ? x.length : 0), 0)
  let budget = diffCap(treeOn(e))
  const commit = o.gitOperation?.commit
  const color = bad ? C.err : stopped ? C.warn : C.accent2
  const { head, rest } = sectionHead(ui, e, {
    key: 'fm-shell', color, mark, markColor: mark === '✗' ? C.err : mark === '✓' ? C.steel : mark === '◷' ? C.accent2 : C.warn,
    label: `❯ ${stopped ? 'Stopped' : bg ? 'Started' : bad ? 'Failed' : 'Ran'}`, subject: desc || lines[0] || '',
    meta: meta.length ? [{ text: meta.join(' · ') }] : [],
  })
  return (
    <Box flexDirection="column" width="100%" marginTop={gapTop(e)} key="fm-shell">
      {head}
      {rest ? <Text key="fm-shell-title" wrap={treeOn(e) ? 'truncate-end' : 'wrap'} color={hex(0xd0d5dd)}>{`  ${rest}`}</Text> : null}
      {shown.map((line, i) => (
        <Box flexDirection="row" key={`fm-shell-cmd-${i}`}>
          <Text color={hex(C.track)}>{'  ┆ '}</Text>
          <Text wrap={cap === 1 ? 'truncate-end' : 'wrap'} color={hex(mix(C.accent, C.dim, 0.45))}>
            {line}
          </Text>
        </Box>
      ))}
      {unseen > 0 && <Text color={hex(C.dim)}>{`  ┆ ${more(unseen, 'command line')}`}</Text>}
      {s.lines.map((l, i) => (
        <Text key={`o-${i}`} color={hex(l.tone === 'plain' ? C.muted : TONE_COLOR[l.tone])} wrap="truncate-end">
          <Text color={hex(C.track)}>{'  │ '}</Text>
          {l.text}
        </Text>
      ))}
      {s.more > 0 && (
        <Text color={hex(C.dim)}>
          {`  └ ${more(s.more, 'line')}`}
          {s.failures ? ' (showing the failures)' : ''}
        </Text>
      )}
      {files.map((f, i) => {
        const rows = diffRows(f.hunks)
        const add = rows.filter(l => l.kind === '+').length
        const del = rows.filter(l => l.kind === '-').length
        const take = takeChanges(rows, Math.max(0, budget))
        budget -= add + del - take.left
        return (
          <Box flexDirection="column" key={`fm-shell-file-${i}`}>
            <Text wrap="truncate-start">
              <Text color={hex(C.edit)}>{'  ✎ '}</Text>
              <Text color={hex(0xd0d5dd)}>{shortPath(clean(String(f.path ?? '')))}</Text>
              <Text color={hex(C.ok)}>{` +${add}`}</Text>
              {del > 0 && <Text color={hex(C.err)}>{` −${del}`}</Text>}
            </Text>
            {diffLines(ui, e, take.rows, `fm-shell-diff-${i}`)}
            {take.left > 0 && <Text color={hex(C.dim)}>{`  └ ${more(take.left, 'changed line')}`}</Text>}
          </Box>
        )
      })}
      {unshown > 0 && <Text color={hex(C.dim)}>{`  ✎ ${more(unshown, 'file')} changed (too large or private to show)`}</Text>}
      {typeof commit?.sha === 'string' && (
        <Text>
          <Text color={hex(C.accent2)}>{'  ⎇ '}</Text>
          <Text color={hex(C.muted)}>committed </Text>
          <Text color={hex(C.accent)}>{commit.sha.slice(0, 7)}</Text>
          {typeof commit.branch === 'string' && <Text color={hex(C.muted)}>{` on ${clean(commit.branch)}`}</Text>}
        </Text>
      )}
      {typeof o.timedOutAfterMs === 'number' && (
        <Text color={hex(C.warn)}>{`  ◷ timed out after ${elapsed(o.timedOutAfterMs)} · moved to the background`}</Text>
      )}
    </Box>
  )
}

/** T-0141: the changed lines a finished Edit or Write shows, or null when its result has no patch to show */
export function editChanges(tool: string, output: unknown) {
  const o = output as { type?: unknown; content?: unknown; structuredPatch?: unknown; userModified?: unknown } | null
  if ((tool !== 'Edit' && tool !== 'Write') || !o || typeof o !== 'object' || !Array.isArray(o.structuredPatch)) return null
  const created = o.type === 'create' && typeof o.content === 'string'
  const hunks = created
    ? [{ oldStart: 1, newStart: 1, lines: String(o.content).replace(/\n$/, '').split('\n').map(l => `+${l}`) }]
    : o.structuredPatch
  const changed = changedLines(hunks)
  return changed.length ? { changed, rows: diffRows(hunks), created, userModified: o.userModified === true } : null
}

/** T-0141: a finished Edit or Write as a Sampo row: its head (the path, +added −removed), then the diff (diffLines).
 * Null without a patch to show: the engine's row then says what happened. */
export function editRow(ui: UI, e: ToolUseRender) {
  const c = editChanges(e.props.tool, e.props.output)
  if (!c) return null
  const { changed, created } = c
  const add = changed.filter(l => l.sign === '+').length
  const del = changed.length - add
  const take = takeChanges(c.rows, diffCap(treeOn(e)))
  const face = toolFace(e.props.tool, e.props.input, cfg.root)
  const { Box, Text } = ui
  const { head, rest } = sectionHead(ui, e, {
    key: 'fm-edit', color: C.edit, mark: '✓', markColor: C.steel, label: `${face.icon} ${e.props.tool === 'Write' ? 'Wrote' : 'Edited'}`,
    subject: face.target,
    meta: [{ text: `+${add}`, color: C.ok }, ...(del ? [{ text: ` −${del}`, color: C.err }] : []),
      ...(created ? [{ text: ' · new file' }] : []), ...(c.userModified ? [{ text: ' · you changed it' }] : [])],
  })
  return (
    <Box flexDirection="column" width="100%" marginTop={gapTop(e)} key="fm-edit">
      {head}
      {rest ? <Text key="fm-edit-path" wrap="truncate-start" color={hex(0xd0d5dd)}>{`  ${rest}`}</Text> : null}
      {diffLines(ui, e, take.rows, 'd')}
      {take.left > 0 && <Text color={hex(C.dim)}>{`  └ ${more(take.left, 'changed line')}`}</Text>}
    </Box>
  )
}

export type AgentOut = { status?: unknown; agentType?: unknown; content?: unknown; totalToolUseCount?: unknown; totalDurationMs?: unknown; totalTokens?: unknown }
export const agentOut = (tool: string, output: unknown) =>
  (tool === 'Agent' || tool === 'Task') && output && typeof output === 'object' &&
  ['completed', 'async_launched'].includes(String((output as AgentOut).status))
    ? (output as AgentOut)
    : null

/** T-0148: a subagent call as a Sampo row: its type and task, tools · time · tokens, the first line of its report
 * (the engine drew the name on a highlighted block); a background launch says so. Null for anything else. */
export function agentRow(ui: UI, e: ToolUseRender) {
  const o = agentOut(e.props.tool, e.props.output)
  if (!o) return null
  const input = (e.props.input ?? {}) as { subagent_type?: unknown; description?: unknown }
  const type = clean(String(input.subagent_type ?? o.agentType ?? 'agent')).replace(/^(?:sampo|foreman):/, '')
  const bg = o.status === 'async_launched'
  const report = Array.isArray(o.content) ? o.content.map(x => String((x as { text?: unknown }).text ?? '')).join('\n') : ''
  const first = clean(report.split('\n').find(l => l.trim()) ?? '').replace(/[*_`#>]/g, '').trim()
  const n = Number(o.totalToolUseCount) || 0
  const meta = bg
    ? 'in the background'
    : [`${n} tool${n === 1 ? '' : 's'}`, elapsed(Number(o.totalDurationMs) || 0), `${Math.round((Number(o.totalTokens) || 0) / 1000)}k tokens`].join(' · ')
  const { Box, Text } = ui
  const { head, rest } = sectionHead(ui, e, {
    key: 'fm-agent', color: C.agent, mark: bg ? '◷' : '✓', markColor: bg ? C.accent2 : C.steel, label: `◆ ${type}`,
    subject: clean(String(input.description ?? '')), meta: [{ text: meta }],
  })
  return (
    <Box flexDirection="column" width="100%" marginTop={gapTop(e)} key="fm-agent">
      {head}
      {rest ? <Text key="fm-agent-desc" wrap="wrap" color={hex(0xd0d5dd)}>{`  ${rest}`}</Text> : null}
      {!bg && first ? bodyLine(ui, 'fm-agent-first', first.slice(0, 200)) : null}
    </Box>
  )
}

// T-1031: tools whose finished rows the engine draws best (a dialog's answers, the todo checklist, a plan)
const ENGINE_ROWS = new Set(['AskUserQuestion', 'TodoWrite', 'ExitPlanMode', 'EnterPlanMode'])

/** The text a tool's result carries: a string, text blocks, or an object's content/text/message, else its plain
 * facts; '' when none. Only its first 4 KB: a row shows a few lines, and this runs on every draw (review). */
export function resultText(output: unknown): string {
  return resultAll(output).slice(0, 4096)
}
function resultAll(output: unknown): string {
  if (typeof output === 'string') return output
  const blocks = (x: unknown) => (Array.isArray(x) ? x.map(b => (typeof b === 'string' ? b : String((b as { text?: unknown })?.text ?? ''))).join('\n') : '')
  if (Array.isArray(output)) return blocks(output)
  if (output && typeof output === 'object') {
    const o = output as { content?: unknown; text?: unknown; message?: unknown; result?: unknown }
    for (const x of [o.content, o.text, o.message, o.result]) {
      const t = typeof x === 'string' ? x : blocks(x)
      if (t.trim()) return t
    }
    // no text: its plain facts, so the row still says what came back (T-1031: the engine drew them in its plain style)
    return Object.entries(output).filter(([, v]) => ['string', 'number', 'boolean'].includes(typeof v))
      .slice(0, 6).map(([k, v]) => `${k}: ${String(v).slice(0, 60)}`).join(' · ')
  }
  return ''
}

/** T-1031: whether toolRow draws this finished call (and its result under it, so the engine's result block blanks) */
export const ownsTool = (tool: string, isErrored: boolean) =>
  !isErrored && !ENGINE_ROWS.has(tool) && !/^(Bash|Edit|MultiEdit|Write|NotebookEdit|Read|Grep|Glob|WebFetch|WebSearch|Agent|Task)$/.test(tool)

/** T-1031: the engine's result block goes under a row toolRow drew, unless the result holds more than text (an image) */
export const ownsResult = (tool: string, isErrored: boolean, output: unknown) =>
  ownsTool(tool, isErrored) && !hasImage(output) && (tool === 'Skill' || shortResult(output) !== null)

/** A result's lines when it is short enough to sit in the row (1-3 lines), else null: a longer or empty one keeps
 * the engine's own block under the row, which the person can expand (review: a 200-line MCP result was cut to 3) */
export function shortResult(output: unknown): string[] | null {
  const all = resultText(output).split('\n').map(l => clean(l).trim()).filter(Boolean) // clean drops newlines: split first
  return all.length >= 1 && all.length <= 3 ? all : null
}

const hasImage = (output: unknown) => {
  const blocks = Array.isArray(output) ? output : output && typeof output === 'object' ? (output as { content?: unknown }).content : null
  return Array.isArray(blocks) && blocks.some(b => !!b && typeof b === 'object' && (b as { type?: unknown }).type === 'image')
}

/** T-1031: any other finished tool ('some things still show as plain like the code review thing'): its section head
 * and the first lines of its result; ReportFindings lists each finding with where it is and what came of it. */
export function toolRow(ui: UI, e: ToolUseRender) {
  if (e.props.isRunning || e.props.isInterrupted || !ownsTool(e.props.tool, !!e.props.isErrored)) return null
  const { Box, Text } = ui
  if (e.props.tool === 'ReportFindings') return findingsRow(ui, e)
  const face = toolFace(e.props.tool, e.props.input, cfg.root)
  const color = face.color === C.dim ? C.accent2 : face.color
  const all = e.props.tool === 'Skill' ? [] : shortResult(e.props.output) ?? [] // a skill's head says it; a long result keeps the engine's block
  const ms = took.get(e.props.tool_use_id)
  const { head, rest } = sectionHead(ui, e, {
    key: 'fm-done', color, mark: '✓', markColor: C.steel, label: `${face.icon} ${face.verb}`, subject: face.target,
    meta: ms !== undefined ? [{ text: elapsed(ms) }] : [],
  })
  return (
    <Box flexDirection="column" width="100%" marginTop={gapTop(e)} key="fm-done">
      {head}
      {rest ? <Text key="fm-done-subject" wrap="wrap" color={hex(0xd0d5dd)}>{`  ${rest}`}</Text> : null}
      {all.map((l, i) => bodyLine(ui, `fm-done-${i}`, l.slice(0, 240)))}
    </Box>
  )
}

type Finding = { file?: unknown; line?: unknown; summary?: unknown; short_summary?: unknown; verdict?: unknown; outcome?: unknown }
const OUTCOME: Record<string, [string, number]> = {
  fixed: ['fixed', C.ok], skipped: ['skipped', C.muted], no_change_needed: ['no change', C.muted],
}

/** T-1031: a review's findings, one line each: what came of it (or how sure it is), where, and the short claim. */
export function findingsRow(ui: UI, e: ToolUseRender) {
  const { Box, Text } = ui
  const input = (e.props.input ?? {}) as { findings?: unknown; level?: unknown }
  const fs = (Array.isArray(input.findings) ? input.findings : []).filter((f): f is Finding => !!f && typeof f === 'object')
  const outcome = (f: Finding) => (Object.hasOwn(OUTCOME, String(f.outcome)) ? OUTCOME[String(f.outcome)] : undefined) // never a prototype key
  const tally = Object.entries(fs.reduce<Record<string, number>>((n, f) => {
    const k = outcome(f)?.[0]
    if (k) n[k] = (n[k] ?? 0) + 1
    return n
  }, {})).map(([k, n]) => `${n} ${k}`).join(' · ')
  const { head } = sectionHead(ui, e, {
    key: 'fm-review', color: C.warn, mark: '✓', markColor: C.steel, label: '◇ Review',
    subject: fs.length ? `${fs.length} finding${fs.length === 1 ? '' : 's'}${typeof input.level === 'string' ? ` at ${clean(input.level)}` : ''}` : 'nothing found',
    meta: tally ? [{ text: tally }] : [],
  })
  const W = 9
  return (
    <Box flexDirection="column" width="100%" marginTop={gapTop(e)} key="fm-review">
      {head}
      {fs.slice(0, 8).map((f, i) => {
        const [word, color] = outcome(f) ?? [String(f.verdict ?? 'found').toLowerCase(), f.verdict === 'CONFIRMED' ? C.err : C.warn]
        const where = `${clean(String(f.file ?? ''))}${typeof f.line === 'number' ? `:${f.line}` : ''}`
        return (
          <Text key={`fm-review-${i}`} wrap="truncate-end">
            <Text color={hex(C.track)}>{'  │ '}</Text>
            <Text color={hex(color)}>{word.padEnd(W).slice(0, W)}</Text>
            <Text color={hex(C.accent)}>{`${shortPath(where, 36)}  `}</Text>
            <Text color={hex(0xd0d5dd)}>{clean(String(f.short_summary ?? f.summary ?? ''))}</Text>
          </Text>
        )
      })}
      {fs.length > 8 && <Text color={hex(C.dim)}>{`  └ ${more(fs.length - 8, 'finding')}`}</Text>}
    </Box>
  )
}


// ------------------------------------------------------------ T-1033: the band's live tree

const PAST: Record<string, string> = {
  Running: 'Ran', Editing: 'Edited', Writing: 'Wrote', Reading: 'Read', Searching: 'Searched', Finding: 'Found',
  Fetching: 'Fetched', 'Searching the web': 'Searched the web',
}

export type TreeIn = {
  feed: readonly FeedItem[]
  subs: readonly LiveAgent[]
  task: string // what the session is on: the task and step
  live: boolean
  now: number
  frame: number | null
  width: number
  rows: number // at most this many rows
  turnMs?: number // how long this turn has run, on the root while it is live
  said?: ReadonlyMap<string, readonly Said[]> // T-1090: what each node last said ('' for this session)
  worktrees?: readonly { agent: string | null; branch: string }[] // T-1090: a builder's worktree, named on its node
}

/** T-1033 ('a tree/graph … each agent's commands/changes under it and yours too, all on the same screen … so the chat
 * doesn't scroll'): this session at the root with its latest calls, then a branch per subagent (running, or done in
 * the last two minutes) with its own; each call marked running, ✓ or ✗ with its time. Within `rows`: the agents keep
 * one call each before the session's older calls go, then the least recent agents fold into a count. */
export function liveTree(ui: UI, e: ResolveInput, t: TreeIn): RenderElement[] {
  const { Box, Text } = ui
  const mine = t.feed.filter(x => x.agent === null && x.icon !== '◆')
  const agents = t.subs.filter(s => !s.done || t.now - (s.lastAt ?? s.startedAt) < TREE_MS)
    .sort((x, y) => Number(x.done) - Number(y.done) || (y.lastAt ?? y.startedAt) - (x.lastAt ?? x.startedAt))
  const callsOf = (id: string) => t.feed.filter(x => x.agent === id)
  const saidOf = (id: string) => t.said?.get(id) ?? []
  const wt = (id: string) => t.worktrees?.find(w => w.agent === id)?.branch
  let k1 = 3
  let k2 = 2
  let kn = 2 // T-1090: lines of the session's own words, then each agent's: fewer as agents multiply
  let ka = agents.length > 2 ? 1 : 2
  let n = agents.length
  const size = () => 1 + Math.min(kn, saidOf('').length) + Math.min(k1, mine.length)
    + agents.slice(0, n).reduce((s, a) => s + 1 + Math.min(ka, saidOf(a.id).length) + Math.min(k2, callsOf(a.id).length), 0)
    + (n < agents.length ? 1 : 0)
  while (size() > t.rows && k2 > 1) k2--
  while (size() > t.rows && k1 > 1) k1--
  while (size() > t.rows && ka > 1) ka--
  while (size() > t.rows && kn > 1) kn--
  while (size() > t.rows && n > 0) n--
  while (size() > t.rows && k2 > 0) k2--
  while (size() > t.rows && k1 > 0) k1--
  while (size() > t.rows && ka > 0) ka--
  while (size() > t.rows && kn > 0) kn--
  const lines = mix(C.track, C.muted, 0.35)
  // T-1039: each node's time bar in one column on a shared axis, as the pane's Agents graph ('more like a tree similar
  // to the agents in the panel'): from the earliest thing shown (at least a minute back) to now
  const starts = [...mine.slice(-Math.max(1, k1)).map(x => x.at), ...agents.slice(0, n).map(a => a.startedAt)]
  const t0 = Math.min(t.now - 60000, ...starts)
  const span = Math.ceil(Math.max(60000, t.now - t0) / 60000) * 60000
  const gw = Math.max(8, Math.min(30, Math.floor(t.width * 0.22)))
  const RW = 18 // review: every row ends in a cell this wide, so the bars before it share one column
  const right = (text: string) => (
    <Box flexShrink={0} width={RW} flexDirection="row" justifyContent="flex-end"><Text wrap="truncate-end" color={hex(C.muted)}>{text}</Text></Box>
  )
  const { Raster } = ui
  const bar = (key: string, row: { start: number; end: number | null; marks: readonly number[]; color: number }, live: boolean) =>
    e.surface === 'terminal'
      ? <Box flexShrink={0}><Raster key={`tree-bar-${key}`} columns={gw} rows={1} cells={ganttCells(row, t.now - span, span, gw, t.now, live ? t.frame : null)} /></Box>
      : null
  const call = (x: FeedItem, prefix: string, key: string) => {
    const done = x.end !== undefined
    const mark = !done ? (t.frame === null ? '▸' : spin(t.frame + x.at)) : x.ok === false ? '✗' : '✓'
    const time = elapsed((x.end ?? t.now) - x.at)
    return (
      <Box key={key} flexDirection="row" gap={1} height={1} overflow="hidden">
        <Box flexShrink={0}><Text color={hex(lines)}>{prefix}</Text></Box>
        <Box flexShrink={0}><Text color={hex(!done ? x.color : x.ok === false ? C.err : C.steel)}>{mark}</Text></Box>
        <Box flexShrink={0}><Text weight="bold" color={hex(x.color)}>{`${x.icon} ${done ? PAST[x.verb] ?? x.verb : x.verb}`}</Text></Box>
        <Box flexGrow={1} flexShrink={1}><Text wrap="truncate-end" color={hex(0xd0d5dd)}>{clean(x.target)}</Text></Box>
        {bar(key, { start: x.at, end: x.end ?? null, marks: [], color: x.color }, !done)}
        {right(time)}
      </Box>
    )
  }
  const rest = agents.length - n
  const branches = [...(k1 ? mine.slice(-k1) : []).map(x => ({ kind: 'call' as const, x })), ...agents.slice(0, n).map(a => ({ kind: 'agent' as const, a })),
    ...(rest ? [{ kind: 'more' as const }] : [])]
  const facts = [mine.length ? `${mine.length} call${mine.length === 1 ? '' : 's'}` : '',
    agents.length ? `${agents.length} agent${agents.length === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ')
  // the root, as the pane's: the session in bold, its task and step (cut, never dropped), every call it made on its bar
  const head = (
    <Box key="tree-root" flexDirection="row" gap={1} height={1} overflow="hidden">
      <Box flexShrink={0}><Text weight="bold" color={hex(C.accent)}>{t.live ? (t.frame === null ? '●' : spin(t.frame)) : '●'}</Text></Box>
      <Box flexShrink={0}><Text weight="bold" color={hex(C.accent)}>this session</Text></Box>
      <Box flexGrow={1} flexShrink={1}><Text wrap="truncate-end" color={hex(0xd0d5dd)}>{t.task}</Text></Box>
      {bar('root', { start: Math.min(t.now, ...mine.map(x => x.at)), end: null, marks: mine.map(x => x.at), color: C.accent }, t.live)}
      {right([facts || 'starting', ...(t.live && t.turnMs ? [elapsed(t.turnMs)] : [])].join(' · '))}
    </Box>
  )
  // T-1090: a node's words, under it and over its calls: a quote mark in its colour, the line in light text
  const words = (id: string, k: number, prefix: string, color: number, key: string) =>
    (k ? saidOf(id).slice(-k) : []).map((w, j) => (
      <Box key={`${key}-said-${w.at}`} flexDirection="row" gap={1} height={1} overflow="hidden">
        <Box flexShrink={0}><Text color={hex(lines)}>{prefix}</Text></Box>
        <Box flexShrink={0}><Text color={hex(color)}>{w.live && t.frame !== null ? spin(t.frame) : '›'}</Text></Box>
        <Box flexGrow={1} flexShrink={1}><Text wrap="truncate-end" color={hex(j === k - 1 || w.live ? 0xc9d1dc : C.muted)}>{w.text}</Text></Box>
      </Box>
    ))
  const out: RenderElement[] = [head, ...words('', kn, branches.length ? '│' : ' ', C.accent, 'tree-root')]
  branches.forEach((b, i) => {
    const last = i === branches.length - 1
    const prefix = last ? '└─' : '├─'
    if (b.kind === 'call') return void out.push(call(b.x, prefix, `tree-${b.x.id}`))
    if (b.kind === 'more') return void out.push(
      <Box key="tree-more" flexDirection="row" gap={1} height={1}>
        <Text color={hex(lines)}>{prefix}</Text>
        <Text color={hex(C.muted)}>{`… ${rest} more subagent${rest === 1 ? '' : 's'}`}</Text>
      </Box>,
    )
    const a = b.a
    const color = agentColor(a.id)
    const took = (a.done ? a.lastAt ?? t.now : t.now) - a.startedAt
    const calls = callsOf(a.id)
    out.push(
      <Box key={`tree-agent-${a.id}`} flexDirection="row" gap={1} height={1} overflow="hidden">
        <Box flexShrink={0}><Text color={hex(lines)}>{prefix}</Text></Box>
        <Box flexShrink={0}><Text color={hex(color)}>{a.done ? '✓' : t.frame === null ? '◆' : spin(t.frame + a.startedAt)}</Text></Box>
        <Box flexShrink={0}><Text weight="bold" color={hex(color)}>{`◆ ${a.type}`}</Text></Box>
        <Box flexGrow={1} flexShrink={1}>
          <Text wrap="truncate-end">
            <Text color={hex(0xd0d5dd)}>{clean(a.description || a.last)}</Text>
            {wt(a.id) ? <Text color={hex(C.accent2)}>{`  ⎇ ${clean(wt(a.id)!)}`}</Text> : null}
          </Text>
        </Box>
        {bar(`agent-${a.id}`, { start: a.startedAt, end: a.done ? a.lastAt ?? t.now : null, marks: a.calls ?? [], color }, !a.done)}
        {right([`${a.tools} call${a.tools === 1 ? '' : 's'}`, ...(took >= 1000 ? [elapsed(took)] : [])].join(' · '))}
      </Box>,
      ...words(a.id, ka, `${last ? ' ' : '│'}  `, color, `tree-agent-${a.id}`),
    )
    const shown = k2 ? calls.slice(-k2) : [] // slice(-0) is the whole list
    shown.forEach((x, j, xs) =>
      out.push(call(x, `${last ? ' ' : '│'}  ${j === xs.length - 1 ? '└─' : '├─'}`, `tree-${a.id}-${x.id}`)))
  })
  return out
}
