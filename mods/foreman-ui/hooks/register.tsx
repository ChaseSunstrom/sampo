import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderElement, RenderInput, ResolveInput } from 'claude-code'


const scrub = atom({ plugin: 'sampo-ui', key: 'scrub' } as const, 0) // T-1081: how many history entries back the pane is showing
const pageAtom = atom({ plugin: 'sampo-ui', key: 'page' } as const, 'board' as 'board' | 'details') // T-3701: the pictures, or every section
import type { FileChurn, FmItem, FmView, LiveAgent, LiveShell, TurnSummary } from '../types'
import {
  C,
  cleanDeep,
  setPalette,
  isBoard,
  checkCells,
  flowCells,
  histCells,
  laneClusters,
  panelAccent,
  MASCOT_COLORS,
  about,
  hoursAgo,
  tokensWord,
  activityCells,
  bucketize,
  cells,
  churnCells,
  laneCells,
  agentColor,
  TONE_COLOR,
  changedLines,
  replyBlocks,
  clean,
  ago,
  clawd,
  elapsed,
  fade,
  ganttCells,
  heat,
  ruleCells,
  narration,
  stackCells,
  stepCells,
  hex,
  humanNext,
  layout,
  miniClawd,
  mix,
  outputSummary,
  paneMark,
  progressCells,
  pulse,
  shortPath,
  sizeWord,
  sparkCells,
  sparkText,
  spin,
  tankCells,
  textBar,
  textComet,
  tone,
  toolFace,
  typeColor,
  typeNum,
  gridRows,
} from './kit'
import { cpuPicture, lanesPicture, libraryPicture, mergePicture, modelsPicture, notePicture, sampoPicture, sessionsPicture, statePicture, taskPicture,
  testsPicture } from './canvas'
import type { CpuIn, Picture } from './canvas'
import { CHECKPOINT_AT, FRAME_MS, FRESH, IDLE_FRAMES, PANE, QUIET_MS, SHELL_MAX_MS, STOP_MS, TIMELINE_MS, TREE_MS, cfg, feed, laneOf, running, said, starts, ticks, took } from './state'
import type { FeedItem } from './state'

const view = atom({ plugin: 'sampo-ui', key: 'view' } as const, null)
const PANE_MIN = 26 // T-1679: the narrowest pane the layout fits (24 inside the padding); below it, one line
const error = atom({ plugin: 'sampo-ui', key: 'error' } as const, null)
const frame = atom({ plugin: 'sampo-ui', key: 'frame' } as const, 0)
const summaries = atom({ plugin: 'sampo-ui', key: 'summaries' } as const, [])
const files = atom({ plugin: 'sampo-ui', key: 'files' } as const, [])
const agents = atom({ plugin: 'sampo-ui', key: 'agents' } as const, [])
const ctx = atom({ plugin: 'sampo-ui', key: 'ctx' } as const, null)
const sound = atom({ plugin: 'sampo-ui', key: 'sound' } as const, true)
const beat = atom({ plugin: 'sampo-ui', key: 'beat' } as const, 0)
const shells = atom({ plugin: 'sampo-ui', key: 'shells' } as const, [])
const away = atom({ plugin: 'sampo-ui', key: 'away' } as const, []) // tasks done since the person last wrote
const resumed = atom({ plugin: 'sampo-ui', key: 'resumed' } as const, null) // T-0145: the resume record acted on
const reviewing = atom({ plugin: 'sampo-ui', key: 'reviewing' } as const, null as string | null) // T-0989: a waiting plan shown in the Task section
import { LIGHT, TREE_INDENT, agentOut, agentRow, bars, branch, chip, editChanges, editRow, fit as fitText, gapTop, kindChip, crestTree, liveTree, mascotTree, meter, miniRail, ownsResult, panelFoot, panelHead, pill, rail, railBox, sectionHead, shellRow, spark, toolRow, treeOn } from './rows'
import type { Motion } from './rows'

// Sampo inside Claude Code. A pure renderer over `fm ui --json` plus what the session's own tool calls show it:
// every rule and gate stays in fm and its classic hooks (which still run under claude -p and older builds); buttons
// only run fm commands or submit a prompt the person pressed for. While work is live a frame clock animates the band,
// the cards and every running tool row; a finished row goes back to the engine, which draws its result as always.
export function toasts(prev: FmView | null, next: FmView): string[] {
  if (!prev?.project || prev.project !== next.project) return []
  const out: string[] = []
  const a = prev.active
  const b = next.active
  if (a && b && a.id === b.id) {
    const was = a.steps.filter(s => s.done).length
    const now = b.steps.filter(s => s.done).length
    if (now > was) {
      const last = b.steps.filter(s => s.done).at(-1)
      out.push(`✓ ${b.id} step ${now}/${b.steps.length}${last ? ` · ${last.text}` : ''}`)
    }
  }
  // Only a task that was open in the last snapshot closed just now (an old closed one edited again is no news).
  const open = new Set([a?.id, ...[...(prev.queue ?? []), ...(prev.inbox ?? [])].map(x => x.id)])
  for (const c of next.closed ?? []) {
    if (open.has(c.id)) out.push(c.status === 'done' ? `✔ ${c.id} done${closeCard(c)}` : `${c.id} ${c.status}`)
  }
  const asked = new Set((prev.approvals ?? []).map(x => `${x.task}:${x.allow.join(',')}`))
  for (const x of next.approvals ?? []) {
    if (!asked.has(`${x.task}:${x.allow.join(',')}`)) out.push(`⚠ ${x.task} needs your yes: ${x.allow.join(', ')}`)
  }
  return out
}

/** T-0473: a close-out's facts after "done": its grade and the lenses it closed with. */
export function closeCard(c: { grade?: string | null; lenses?: string[] }): string {
  const parts = [c.grade, (c.lenses ?? []).join(', ')].filter(Boolean)
  return parts.length ? ` · ${parts.join(' · ')}` : ''
}

/** T-0472: the band's one glyph: the worst signal (red before amber) and how many more, or null when calm. */
export function signalLine(v: FmView | null): { level: 'red' | 'amber'; text: string } | null {
  const sig = v?.signals ?? []
  const worst = sig.find(x => x.level === 'red') ?? sig[0]
  if (!worst) return null
  return { level: worst.level, text: worst.text + (sig.length > 1 ? ` +${sig.length - 1}` : '') }
}

/** The guard's refusal text, when a tool call was refused by Ward. */
export function guardReason(r: { deny?: string; isError?: true; text?: string }): string | undefined {
  const why = r.deny ?? (r.isError ? r.text : undefined)
  const m = why?.match(/^(?:\S+ hook error: )?(?:Foreman|Sampo|Ward)(?: guard)?:\s*([\s\S]*)/) // a refusal leads with it, output never
  return m ? m[1]!.slice(0, 120) : undefined
}

/** The closing line's extra: what the turn did. */
export function summaryText(s: TurnSummary): string {
  const parts = [`${s.tools} tool${s.tools === 1 ? '' : 's'}`]
  if (s.edits) parts.push(`${s.edits} edit${s.edits === 1 ? '' : 's'} +${s.add} −${s.del}`)
  if (s.agents) parts.push(`${s.agents} subagent${s.agents === 1 ? '' : 's'}`)
  if (s.step) parts.push(`✓ ${s.step}`)
  return parts.join(' · ')
}

/** A plain Sampo bookkeeping command (`fm …`, maybe after a `cd`): its row folds to one line once it succeeds.
 * Anything chained, piped, substituted or on a second line keeps the full row, so the fold never hides what ran. */
export function fmCommand(command: unknown): string | null {
  if (typeof command !== 'string') return null
  const m = /^(?:cd\s+[^\s;&|`$<>]+\s*&&\s*)?fm\s+([^;&|`$<>\n\r]+)$/.exec(command.trim())
  return m && m[1]!.length <= 160 ? m[1]!.replace(/\s+/g, ' ') : null
}

/** A Sampo permission request (`fm ask ID cats --why "…"`), in plain words for a note under its dialog. */
export function askNote(command: unknown): string | null {
  if (typeof command !== 'string') return null
  // the reason may hold no quote, $, backtick, backslash or newline: then the whole command is the one ask shown
  const m = /^fm\s+ask\s+(T-\d{4,})\s+([\w\s-]+?)\s+(?:--pin\s+[\w@.:/-]+\s+)?--why\s+(?:"([^"$`\\\n]*)"|'([^'\n]*)')$/.exec(
    command.trim(),
  )
  const why = m ? (m[3] ?? m[4] ?? '') : ''
  if (!m || why.length > 160) return null // too long to show whole: no summary rather than a cut one
  return `⚠ Sampo asks: a yes grants ${m[2]!.trim().split(/\s+/).join(' + ')} for ${m[1]} — ${why.replace(/\s+/g, ' ')}`
}

/** One line for a finished read: how much it read. */
export function readSummary(tool: string, output: unknown): string {
  const o = (output && typeof output === 'object' ? output : {}) as { file?: { numLines?: number; totalLines?: number } }
  if (tool === 'Read' && typeof o.file?.numLines === 'number') {
    const total = o.file.totalLines
    return total && total > o.file.numLines ? `${o.file.numLines} of ${total} lines` : `${o.file.numLines} lines`
  }
  return ''
}

/** The last meaningful line a command printed. */
export function lastLine(output: unknown): string {
  const o = (output && typeof output === 'object' ? output : {}) as { stdout?: unknown; stderr?: unknown }
  const text = [o.stdout, o.stderr].filter(x => typeof x === 'string').join('\n')
  return (text.split('\n').map(l => clean(l).trim()).filter(Boolean).at(-1) ?? '').slice(0, 100)
}

/** Whether something waits on the person: a yes, or a plan to approve. */
const waiting = (v: FmView | null) =>
  !!v && ((v.approvals ?? []).length > 0 || (v.queue ?? []).some(q => q.waits === 'plan approval'))

/** T-3664: the work in lanes as the band's and the queue card's words, empty parts left out. */
const flowWords = (v: FmView) => {
  const l = v.lanes
  return l
    ? [l.building.length ? `${l.building.length} building` : '', l.merging.length ? `${l.merging.length} to merge` : '',
       l.closing.length ? `${l.closing.length} to close` : ''].filter(Boolean)
    : []
}

// Module variables start over on a reload; what a drawing reads lives in $.state.
let fmPath: string | null = null
let marks: string | null = null // the busy watched files' stamps (null: not seen yet)
let hotMarks: string | null = null // T-3580: the stamps of what the user acts on (fm ui's watch_now)
let stale = false // T-3580: a busy file moved since the last run; fm ui reruns once GAP_MS allows
let owed = false // T-3580: a watch_now move or a swallowed turn-end or pane refresh: the next poll past URGENT_MS runs it
const GAP_MS = 10000 // T-3580: busy files and tool calls rerun fm ui (~0.5 s CPU) at most this often
const URGENT_MS = 2000 // T-3580: and what the user acts on at most this often
let agentsShown = '' // T-1437: the running subagents' times as last drawn, when no mascot blink repaints them
let isDirty = true
let saidSeq = 0 // T-1090: one id per model step, for its narration entry
let isBusy = false
let lastFull = 0
let poll: { cancel: () => void } | null = null
let clock: { cancel: () => void } | null = null
let lastActive = 0 // the frame of the last turn or tool activity
let checkpointed = false
let economyOn = false
let weeklyLean = true // T-3049: fm ui's weekly_lean: false once the project turned the weekly lean off
let happyUntil = 0 // the mascot jumps for a few seconds after a task closes
let bgLive = 0 // background shells still running: the clock keeps a calm pace for the waiting row
let treeTall = 0 // T-1039: the band tree's height this turn: it grows when it must and never shrinks mid-turn
let treeDrawn = false // T-1039 review: the band drew the live tree, so the spinner line would repeat it
let calm = 0
const turns = new Set<string>()
const agentCalls = new Map<string, string>() // running Agent call → its description
const agentOf = new Map<string, string>() // running Agent call → the subagent's id
let turn = { tools: 0, edits: 0, add: 0, del: 0, agents: 0, stepsDone: -1, task: '' }
let turnStartedAt = 0 // the working line's clock (T-0143)
const seenReplies = new Set<string>() // reply texts drawn during a turn; the newest one's mark animates
let newestReply = ''
const DOING: Record<string, string> = { requesting: 'thinking', thinking: 'thinking', responding: 'writing', 'tool-input': 'preparing a tool call', 'tool-use': 'running tools' }

async function fm($: EngineInterface): Promise<string> {
  if (fmPath) return fmPath
  const given = await $.env.get('FOREMAN_FM')
  const home = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${(await $.env.get('HOME')) ?? ''}/.claude`
  const installed = `${home}/foreman/plugin/bin/fm`
  fmPath = given ?? ((await $.fs.exists(installed).catch(() => false)) ? installed : 'fm')
  return fmPath
}

async function run($: EngineInterface, args: string[]) {
  return $.process.run([await fm($), ...args], { timeoutMs: 15000 })
}

async function chime($: EngineInterface, name: 'done' | 'needs') {
  if (await read($, sound)) await $.audio.play({ asset: `sounds/${name}.wav` }).catch(() => undefined)
}

// fromPoll: the 2 s poll's own run; any other (a turn's end, a pane action, a command) is one the user waits on
async function refresh($: EngineInterface, fromPoll = false): Promise<boolean> {
  if (isBusy) {
    if (fromPoll) isDirty = true
    else owed = true // T-3580: a run already in flight swallowed it; the next poll runs it, not the 10 s gap
    return false
  }
  isBusy = true
  isDirty = false
  try {
    const r = await run($, ['ui', '--json'])
    if (r.exitCode !== 0) throw new Error(r.stderr.trim().split('\n').at(-1) || `fm ui exited ${r.exitCode}`)
    const next = cleanDeep(JSON.parse(r.stdout)) as FmView // T-1679: no title or name draws a control or bidi override
    savedPalette = next.palette_custom // T-1780: kept, so an options change paints it too
    weeklyLean = next.weekly_lean !== false // T-3049
    if (cfg.palette === 'default') setPalette('default', savedPalette) // T-1717: a saved palette paints over
    const prev = await read($, view)
    const was = prev?.active
    const now = next.active
    if (cfg.motion !== 'off' && was && now && was.id === now.id) { // a glow needs the clock to fade
      const t = await $.clock.now()
      for (const s of now.steps)
        if (s.done && !was.steps.find(x => x.n === s.n)?.done) {
          for (const [k, at] of flashed) if (t - at > FLASH_MS) flashed.delete(k)
          flashed.set(`${now.id}:${s.n}`, t)
          flashUntil = t + FLASH_MS
        }
      if (flashUntil > t) await wake($)
    }
    const lines = toasts(prev, next)
    for (const line of lines) $.ui.toast(line)
    if (lines.some(l => l.startsWith('⚠'))) await chime($, 'needs')
    else if (lines.some(l => l.startsWith('✔'))) await chime($, 'done')
    const closed = lines.map(l => /^✔ (T-\d+) done/.exec(l)?.[1]).filter((x): x is string => !!x)
    if (closed.length) {
      const t = await $.clock.now()
      const was = prev?.active?.id === closed[0] ? prev.active : null
      const rec = (next.closed ?? []).find(x => x.id === closed[0])
      closing = {
        id: closed[0]!, title: was?.title ?? rec?.title ?? '', grade: rec?.grade, lenses: rec?.lenses,
        proved: was ? was.criteria.length : null, // done means every criterion has its evidence
        minutes: typeof was?.on_task_s === 'number' ? (was.on_task_s * 1000 + Math.max(0, t - lastFull)) / 60000 : null,
        usual: was ? prev?.typical?.[`${was.type}/${was.tier}`] : undefined, at: t,
      }
      if (cfg.motion !== 'off') {
        flashUntil = Math.max(flashUntil, t + CLOSING_MS) // the clock runs until the card has settled away
        await wake($)
      }
    }
    if (closed.length) await update($, away, list => [...list, ...closed.filter(x => !list.includes(x))])
    if (lines.some(l => l.startsWith('✔'))) {
      happyUntil = (await $.clock.now()) + 6000
      $.clock.after(500, () => void freshen($)) // outside this dispatch
    }
    const bs = next.brainstorm // T-1023: when each answer came in, for the Agents graph
    if (bs && (bs.running || (ideas?.name === bs.name && ideas.end === undefined))) {
      const t = await $.clock.now()
      if (bs.running && ideas?.name !== bs.name) ideas = { name: bs.name, at: t, answers: [] }
      if (ideas) {
        while (ideas.answers.length < bs.answers) ideas.answers.push(t)
        if (!bs.running) ideas.end = t
      }
    }
    await update($, view, () => next)
    await update($, error, () => null)
    lastFull = await $.clock.now()
    return true
  } catch (err) {
    await update($, error, () => String(err instanceof Error ? err.message : err).slice(0, 200))
    return false
  } finally {
    isBusy = false
  }
}

// T-1574: the options the mod started with; the poll applies the engine's current ones over them when they change
let startOptions: Record<string, unknown> = {}
let seenOptions = ''
// T-1613: each option's usable values. One the pane can't use keeps its last good value (kept across restarts in the
// mod's store) and is said once, as fm code's app keeps its last good tui.toml (T-1543)
const OPTION_OK: Record<string, (v: unknown) => boolean> = {
  freshAt: v => typeof v === 'number',
  freshTokens: v => typeof v === 'number',
  mascot: v => typeof v === 'string' && (v === 'off' || v === 'sampo' || v in MASCOT_COLORS),
  motion: v => v === 'full' || v === 'reduced' || v === 'calm' || v === 'off',
  chat: v => v === 'full' || v === 'tree',
  palette: v => v === 'default' || v === 'cb' || v === 'riso', // fm code's words (settings.SHARED)
  look: v => v === 'board' || v === 'forge', // T-3701: the Board's, or the forge look before it
}
let goodOptions: Record<string, unknown> = {}

/** T-1613: the options as the pane takes them (each usable value, else the last good one) and the ones it kept */
export function usable(options: Record<string, unknown>, good: Record<string, unknown>) {
  const out: Record<string, unknown> = { ...options }, kept: string[] = []
  for (const [k, ok] of Object.entries(OPTION_OK)) {
    if (options[k] === undefined || ok(options[k])) continue
    kept.push(k)
    if (ok(good[k])) out[k] = good[k]
    else delete out[k]
  }
  return { out, kept }
}
let savedPalette: Record<string, unknown> | undefined // T-1780: fm ui's last palette_custom (T-1717)

// T-1574: /config or fm settings set (settings.json) changed an option: it applies on this poll, no restart. The first
// poll applies the engine's values too, so a change made before it is never only recorded.
async function pollOptions($: EngineInterface) {
  const rows = await $.config.list()
  if (!Array.isArray(rows)) return
  const now: Record<string, unknown> = {}
  for (const r of rows) {
    const m = /^(?:sampo|foreman)-ui(?:@[^.]+)?\.(.+)$/.exec(r.key) // T-2879: the mod under either name
    if (m) now[m[1]!] = r.value
  }
  const seen = JSON.stringify(now)
  if (seen === seenOptions) return
  seenOptions = seen
  const { out, kept } = usable({ ...startOptions, ...now }, goodOptions)
  const wasBoard = isBoard()
  applyOptions(out)
  // T-3701: an open pane takes the new palette's ground (each open sets it anew)
  if (wasBoard !== isBoard() && (await $.ui.panes().catch(() => [])).some(p => p.id === PANE && p.isShown)) await openPane($, false).catch(() => undefined)
  for (const k of Object.keys(OPTION_OK)) if (OPTION_OK[k]!(out[k])) goodOptions[k] = out[k]
  await $.store.set('goodOptions', goodOptions).catch(() => undefined)
  const show = (v: unknown) => clean(typeof v === 'string' ? `'${v}'` : JSON.stringify(v) ?? '').slice(0, 40)
  if (kept.length) $.ui.toast(`⚠ Sampo: ${kept.map(k => `${k} ${show(now[k] ?? startOptions[k])} isn't a value it can use; kept ${clean(String(out[k] ?? 'the default'))}`).join('; ')} (fm settings list shows the choices)`)
  if (cfg.motion === 'off') sleep() // T-0912: off means no clocks, even mid-work
  else if (cfg.motion === 'reduced') { // the 8 fps frame clock only: the 30 fps blit stops now
    motionClock?.cancel()
    motionClock = null
  }
  await update($, frame, n => n + 1) // redrawn in the new palette and motion
}

// Cheap in-process checks every 2 s: the context meter, the options, and the paths fm names (fm runs only when one moved).
async function tick($: EngineInterface) {
  await gauge($).catch(() => undefined)
  await pollOptions($).catch(() => undefined) // an engine without $.config: the rest of the poll still runs
  if ((await read($, shells)).length) await noteShells($, list => list) // ages out lost ones; a reload re-arms bgLive
  const running = (await read($, agents)).filter(a => !a.done)
  if (cfg.mascot !== 'off') await update($, beat, n => n + 1) // the mascot's idle blink, at the poll's pace
  else {
    // T-1437: no blink to repaint on, so a running subagent's time would freeze; repaint when what it shows changes
    const t = await $.clock.now()
    const shown = running.map(a => {
      const quiet = t - (a.lastAt ?? a.startedAt)
      return quiet >= QUIET_MS ? `quiet ${elapsed(quiet)} ${quiet >= STOP_MS}` : elapsed(t - a.startedAt)
    }).join('|')
    if (shown !== agentsShown) await update($, beat, n => n + 1)
    agentsShown = shown
  }
  const listed = running.length ? await $.agent.list().catch(() => null) : null
  if (listed) {
    // T-0121: one that ended without its turn.complete (killed, a compaction, a reload) is no longer running in the
    // engine's own list, or not in it at all: it ends here instead of reading as running for hours
    const live = new Set(listed.filter(a => a.status === 'running').map(a => a.id))
    if (running.some(a => !live.has(a.id))) await update($, agents, list => list.map(a => (live.has(a.id) ? a : { ...a, done: true })))
  }
  const v = await read($, view)
  const stamps = async (paths?: string[]) =>
    (await Promise.all((paths ?? []).map(p => $.fs.stat(p).then(s => `${s.mtimeMs}:${s.size}`, () => '-')))).join('|')
  const [slow, hot] = await Promise.all([stamps(v?.watch), stamps(v?.watch_now)])
  const now = await $.clock.now()
  // T-3580: the ledger and the tasks folder move with every tool call (fm ui ran ~1,665 times an hour): those and the
  // tool calls rerun it at most every GAP_MS; a pause, trust, an ask, drive or the active task's brief on the next poll
  // URGENT_MS after the last run (a burst of them is one run)
  if (hotMarks !== null && hot !== hotMarks) owed = true
  hotMarks = hot
  if (marks !== null && slow !== marks) stale = true
  marks = slow
  const since = now - lastFull
  if (owed ? since >= URGENT_MS : ((isDirty || stale) && since >= GAP_MS) || since > 30000) {
    const moved = stale
    const urgent = owed
    stale = false
    owed = false
    await refresh($, !urgent) // an owed run a run in flight swallows stays owed
    // T-3542: fm kept its view (up to 10 s old), so the move this poll saw may not be in it yet: ask again after the gap
    if (moved && (await read($, view))?.cached) stale = true
  }
}

// The context meter; at CHECKPOINT_AT% Sampo checkpoints the active task once, so a compaction loses nothing.
async function gauge($: EngineInterface) {
  const u = await $.session.usage().catch(() => null)
  const eco = economyText(u?.rateLimits ?? [], weeklyLean)
  if (eco && !economyOn) $.ui.toast('Usage is high · Sampo economy mode: leaner work until the window resets', { timeoutMs: 10000 })
  economyOn = !!eco
  const percent = u?.context.percent
  if (typeof percent !== 'number') return
  const prev = await read($, ctx)
  if (!prev || Math.round(prev.percent) !== Math.round(percent)) await update($, ctx, () => ({ percent }))
  if (percent < 60) checkpointed = false // a /compact brought it down: the next climb checkpoints again
  if (percent >= CHECKPOINT_AT && !checkpointed && (await read($, view))?.active) {
    checkpointed = true
    const r = await run($, ['checkpoint']).catch(err => ({ exitCode: 1, stderr: String(err), stdout: '' }))
    if (r.exitCode !== 0) checkpointed = false // try again on the next poll
    $.ui.toast(
      r.exitCode === 0
        ? `Context ${Math.round(percent)}% · Sampo checkpointed: /compact resumes at this exact step`
        : `Context ${Math.round(percent)}% · checkpoint failed: ${lastLine({ stderr: r.stderr })}`,
      { timeoutMs: 10000 },
    )
  }
}

// T-0198: the person is usage-sensitive; past these shares of a usage window Claude works leaner until it resets.
const ECONOMY: Record<string, [number, string]> = { seven_day: [80, 'weekly'], five_hour: [90, '5-hour'] }

// The economy section, or null: it names the threshold, not the live figure, so the prompt (and its cache) changes
// only when a window crosses it.
// T-3049: weekly false: the project turned the weekly lean off (sampo drive on --pace off); the 5-hour window still counts.
export function economyText(limits: readonly { kind: string; percentUsed: number }[], weekly = true): string | null {
  const hot = limits.filter(l => ECONOMY[l.kind] && l.percentUsed >= ECONOMY[l.kind][0] && (weekly || l.kind !== 'seven_day'))
  if (!hot.length) return null
  const what = hot.map(l => `${ECONOMY[l.kind][1]} usage is past ${ECONOMY[l.kind][0]}%`).join(' and ')
  return (
    `# Sampo economy mode\nThis account's ${what}. Work leaner until it resets: no brainstorms or multi-round ` +
    `fm ideas, at most one combined reviewer pass per task (none for S), no other subagents unless the task can't ` +
    `go on without one, targeted reads (grep, fm outline, line ranges), fm check --affected while iterating, short ` +
    `replies. Correctness, evidence and the guard's rules don't change.`
  )
}

// A task just closed: with the context past freshAt percent or freshTokens tokens, compact so the next task starts on
// what still matters (T-0098). Every turn re-reads the whole context, so tokens, not a share of a 1M window, set the
// cost (T-0703).
async function freshen($: EngineInterface) {
  if (!cfg.freshAt && !cfg.freshTokens) return
  const context = (await $.session.usage().catch(() => null))?.context
  const percent = context?.percent, tokens = context?.tokens
  const byPct = !!cfg.freshAt && typeof percent === 'number' && percent >= cfg.freshAt
  const byTokens = !!cfg.freshTokens && typeof tokens === 'number' && tokens >= cfg.freshTokens
  if (!byPct && !byTokens) return
  const at = typeof tokens === 'number' ? `${Math.round(tokens / 1000)}k tokens` : `${Math.round(percent ?? 0)}%`
  $.ui.toast(`Task closed at context ${at} · compacting so the next task starts fresh`, { timeoutMs: 8000 })
  await $.session.compact({ instructions: FRESH }).catch(() => undefined)
}

async function act($: EngineInterface, args: string[], done: string) {
  const r = await run($, args)
  $.ui.toast(r.exitCode === 0 ? done : `fm ${args[0]}: ${(r.stderr || r.stdout).trim().split('\n').at(-1)}`)
  await refresh($)
}

async function capture($: EngineInterface, text: string) {
  const idea = text.trim()
  if (!idea) return
  const r = await run($, ['capture', idea])
  const id = /T-\d{4,}/.exec(r.stdout)?.[0]
  $.ui.toast(r.exitCode === 0 ? `⚑ Captured ${id ?? ''} — ${idea.slice(0, 60)}` : `fm capture: ${lastLine({ stderr: r.stderr })}`)
  await refresh($)
}

// T-0711: one steer for every live session here and on the remotes the user added (fm conductor steer)
async function steer($: EngineInterface, text: string) {
  const words = text.trim()
  if (!words) return
  const r = await run($, ['conductor', 'steer', words])
  $.ui.toast(r.exitCode === 0 ? `↯ ${lastLine({ stdout: r.stdout }) || 'Steer sent'}` : `fm conductor: ${lastLine({ stderr: r.stderr })}`)
  await refresh($)
}

const DOCK_COLS = 104
async function openPane($: EngineInterface, focus: boolean) {
  // ~76 columns docked: the cards fit, and the transcript and band keep the rest (T-0122: the share left them ~37);
  // T-3701: 104 since the board page: two columns of its nine panels (at most 45% of the terminal, the host's cap)
  // T-3701: in the Board's palette the pane paints the Board's near-black ground; the other palettes keep the terminal's
  const bg = isBoard() ? { backgroundColor: hex(C.bg) } : {}
  return $.ui.open(focus ? { id: PANE, title: 'Sampo', focus: true, columns: DOCK_COLS, ...bg } : { id: PANE, title: 'Sampo', columns: DOCK_COLS, ...bg })
}

// T-0909: the pane's moving rasters (the rail's comet, the live lane's glow) repaint by blit at ~30 fps while work is
// live, without redrawing the pane; the pane registers them as it draws and the 8 fps frame clock keeps the rest.
const motion: Motion = new Map()
const MOTION_MS = 33
let motionClock: { cancel: () => void } | null = null
const frameAt = (ms: number) => ms / FRAME_MS // time in frames, fractional: what a comet takes
// T-1000: the kinds of tool call the Live section's lanes chart, and their colours
const KINDS = ['edit', 'run', 'read', 'agent', 'skill', 'web'] as const
// names in C, so a lane's colour follows the palette (T-1512)
const KIND_COLOR: Record<string, keyof typeof C> = { edit: 'edit', run: 'accent', read: 'accent2', agent: 'agent', skill: 'skill', web: 'web' }
// A step that just got done glows green and fades over FLASH_MS; the frame clock runs until it has
const flashed = new Map<string, number>() // "T-0007:2" → when it was seen done
const FLASH_MS = 2500
const BG = 0x181a21 // the terminal's background the glow fades into
let flashUntil = 0
// T-0913: the task that just closed, shown for CLOSING_MS at the top of the pane: grade, proof, time against the usual
type Closing = { id: string; title: string; grade?: string | null; lenses?: string[]; proved: number | null; minutes: number | null; usual?: number; at: number }
let closing: Closing | null = null
let ideas: { name: string; at: number; answers: number[]; end?: number } | null = null // T-1023: the brainstorm's answers over time
const CLOSING_MS = 6000
const SWEEP_MS = 900

let painting = false
async function repaint($: EngineInterface) {
  if (painting) return // a slow blit: skip a tick rather than pile them up
  if (!turns.size && !starts.size) {
    motionClock?.cancel() // live work ended: everything settles on the next redraw
    motionClock = null
    return
  }
  painting = true
  try {
    const f = frameAt(await $.clock.now())
    for (const [key, m] of [...motion]) {
      const r = await $.ui.blit({ requestId: PANE, key, columns: m.columns, rows: m.rows, cells: m.draw(f) }).catch(() => ({ deny: 'failed' }))
      if (r && 'deny' in r && r.deny) motion.delete(key) // not mounted (pane closed, redrawn smaller): until it draws again
    }
  } finally {
    painting = false
  }
}

// The frame clock runs only while work is live: a turn, or a tool call (a subagent's included).
async function wake($: EngineInterface) {
  lastActive = await read($, frame)
  if (cfg.motion === 'off') return // T-0912: a still UI; the 2 s poll still brings every change
  if (!motionClock && cfg.motion === 'full') motionClock = $.clock.every(MOTION_MS, () => void repaint($))
  if (clock) return
  clock = $.clock.every(FRAME_MS, () => void advance($))
}

async function advance($: EngineInterface) {
  const f = await read($, frame)
  if (starts.size) lastActive = f // a long build is live work: its row keeps moving (finally always clears starts)
  const now = await $.clock.now()
  const quiet = !turns.size && !starts.size && now >= flashUntil // a step's glow is still fading
  if (quiet && !bgLive && f - lastActive > 2) {
    await update($, frame, n => n + 1) // one last redraw, so what faded (a glow, the done card) is drawn settled
    return sleep()
  }
  if (!bgLive && f - lastActive > IDLE_FRAMES) return sleep()
  if (quiet && bgLive && ++calm % 4) return // only background work: a calmer pace (~2 frames a second)
  await update($, frame, n => n + 1)
}

function sleep() {
  clock?.cancel()
  clock = null
  motionClock?.cancel() // T-0909 review: a lost turn.complete must not leave the 30 fps blit running for good
  motionClock = null
}

// A subagent's tool calls carry its id: count them and tie the subagent to the Agent row that started it.
async function noteSubagent($: EngineInterface, agentId: string, last: string) {
  const known = (await read($, agents)).find(a => a.id === agentId)
  const lastAt = await $.clock.now()
  if (known) {
    await update($, agents, list => list.map(a => (a.id === agentId ? { ...a, tools: a.tools + 1, last, lastAt, calls: [...(a.calls ?? []), lastAt].slice(-300) } : a)))
    return
  }
  const info = (await $.agent.list()).find(a => a.id === agentId)
  const call = [...agentCalls].find(([id, d]) => d === info?.description && !agentOf.has(id))
  if (call) agentOf.set(call[0], agentId)
  const fresh: LiveAgent = {
    id: agentId,
    type: info?.type ?? 'subagent',
    description: info?.description ?? '',
    startedAt: (call && starts.get(call[0])) ?? lastAt, // from its Agent call, when that is known
    lastAt,
    tools: 1,
    last,
    done: false,
    calls: [lastAt],
  }
  // T-1023: finished ones stay for the Agents graph's window
  await update($, agents, list => [...list.filter(a => !a.done), ...list.filter(a => a.done && lastAt - (a.lastAt ?? a.startedAt) < TIMELINE_MS).slice(-8), fresh])
}

// Background shells: a Bash call that came back with a task id (run_in_background, ctrl+b, or its timeout) is running
// until its notification arrives, TaskStop stops it, or it is older than any background command may run.
async function noteShells($: EngineInterface, fn: (list: LiveShell[]) => LiveShell[]) {
  const now = await $.clock.now()
  await update($, shells, list => fn(list).filter(x => now - x.startedAt < SHELL_MAX_MS))
  bgLive = (await read($, shells)).length
  if (bgLive) await wake($)
}

/** The pane's Stop on a subagent that has gone quiet: the same TaskStop Claude would call. */
async function stopAgent($: EngineInterface, id: string) {
  await $.tool.call({ tool: 'TaskStop', task_id: id }).catch(() => undefined)
  await update($, agents, list => list.map(a => (a.id === id ? { ...a, done: true } : a)))
}

async function noteEdit($: EngineInterface, path: string, add: number, del: number) {
  await update($, files, list => {
    const was = list.find(f => f.path === path)
    const row: FileChurn = { path, add: (was?.add ?? 0) + add, del: (was?.del ?? 0) + del, edits: (was?.edits ?? 0) + 1 }
    return [...list.filter(f => f.path !== path), row].slice(-12)
  })
}

/** T-0145: Claude Code hot-reloads this mod only when a turn really ends, so a driven turn that changed it ends on
 * purpose and leaves a resume record; the reloaded mod (session.start runs again) starts the next turn itself, once
 * per record and only in the session the record names. */
async function resumeAfterReload($: EngineInterface) {
  const r = (await read($, view))?.resume_after_reload
  if (!r || r.at === (await read($, resumed)) || r.session !== (await $.session.id())) return
  await update($, resumed, () => r.at)
  $.ui.toast('Sampo UI reloaded · the drive continues', { timeoutMs: 6000 })
  void $.prompt.submit({ text: `Continue the Sampo drive: the Sampo UI reloaded${r.task ? ` (${r.task})` : ''}` })
}

/** The quick-capture box (every surface with text input). */
function captureBox($: EngineInterface, e: ResolveInput) {
  if (e.surface === 'mobile') return null
  const { Input } = $.ui.resolve(e)
  return (
    <Input key="capture" placeholder="⚑ capture an idea for later… (Enter)" submitLabel="Capture" onSubmit={text => void capture($, text)} />
  )
}

function applyOptions(options: Record<string, unknown>) {
  cfg.freshAt = typeof options.freshAt === 'number' ? options.freshAt : 40
  cfg.freshTokens = typeof options.freshTokens === 'number' ? options.freshTokens : 200000
  cfg.mascot = typeof options.mascot === 'string' && (options.mascot === 'off' || options.mascot === 'sampo' || options.mascot in MASCOT_COLORS) ? options.mascot : 'sampo' // T-3113: the crest is the default
  // T-0912: full = 30 fps charts by blit; reduced = the 8 fps frame clock only; off = no clocks, a still pane.
  // T-1496: reduced is fm code's word too; the old calm still means it
  cfg.motion = options.motion === 'reduced' || options.motion === 'calm' ? 'reduced' : options.motion === 'off' ? 'off' : 'full'
  cfg.chat = options.chat === 'full' ? 'full' : 'tree' // T-1040
  // T-1081, T-1512; T-3701: palette keeps fm code's words (default, cb, riso); the look picks what default draws on
  cfg.palette = options.palette === 'cb' || options.palette === 'riso' ? options.palette : options.look === 'forge' ? 'forge' : 'default'
  setPalette(cfg.palette, savedPalette) // T-1780: the saved one too, or an options change dropped it until a refresh
}

export const register: Register = (on, options) => {
  startOptions = options
  seenOptions = ''
  applyOptions(options)
  on('session.start', async ($, e, next) => {
    cfg.root = typeof e.cwd === 'string' ? e.cwd : ''
    feed.length = 0 // T-1033 review: a new session's tree starts empty
    treeTall = 0 // T-1041: and at its own height
    said.clear() // T-1090: and so do its nodes' words
    await $.command.register({ name: 'fm', description: 'Sampo: open or close the dashboard pane' })
    await $.command.register({
      name: 'fm-trust',
      description: 'Ward: let Claude edit the guard and Claude Code settings (on | off); works only when you type it',
    })
    const good = await $.store.get('goodOptions').catch(() => undefined) // T-1613: the last good options, checked
    goodOptions = good && typeof good === 'object' && !Array.isArray(good) ? usable(good as Record<string, unknown>, {}).out : {}
    applyOptions(usable(startOptions, goodOptions).out)
    const stored = await $.store.get('sound').catch(() => undefined)
    if (stored === false) await update($, sound, () => false)
    const kept = await $.store.get('page').catch(() => undefined) // T-3701: the page the person left the pane on
    if (kept === 'details' || kept === 'board') await update($, pageAtom, () => kept)
    await refresh($)
    await resumeAfterReload($)
    poll?.cancel() // one poller per environment, however often the session starts
    poll = $.clock.every(2000, () => void tick($))
    if ((await read($, view))?.project) void openPane($, false) // seats unasked only on a wide fullscreen terminal
    return next(e)
  })

  on('command.run', { command: 'fm' }, async $ => {
    const shown = (await $.ui.panes()).some(p => p.id === PANE && p.isShown)
    if (shown) {
      await $.ui.close({ id: PANE })
      return { text: 'Sampo pane closed.' }
    }
    await refresh($)
    const opened = await openPane($, true)
    return { text: opened.isPlaced ? 'Sampo pane opened.' : `Sampo pane: ${opened.reason}` }
  })

  // T-0120: only the person's own Enter (or their phone, via the bridge) turns trust on; a prompt the agent scheduled, a
  // skill call or another plugin's run is refused. On writes the record into Sampo state straight from here (no tool
  // call may write there, and fm has no "on"); fm trust then records it in the ledger, and off is fm's.
  on('command.run', { command: 'fm-trust' }, async ($, e) => {
    const kind = e.origin?.kind
    if (kind !== 'composer' && kind !== 'bridge') return { text: 'Ward: /fm-trust works only when you type it.' }
    const arg = e.args.trim()
    if (arg === 'on') {
      const home = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${(await $.env.get('HOME')) ?? ''}/.claude`
      const path = (await read($, view))?.trust_file ?? `${home}/foreman/state/trust.json`
      await $.fs.write(path, JSON.stringify({ on: true, at: new Date(await $.clock.now()).toISOString().slice(0, 19) + 'Z', via: kind }))
    }
    const r = await run($, ['trust', ...(arg === 'off' ? ['off'] : [])])
    isDirty = true
    await refresh($)
    return { text: `${r.stdout.trim() || r.stderr.trim()}`.split('\n').at(-1) ?? '' }
  })

  // T-3752: the risk budget's off switch, the same way: only the person's typed Enter writes the record (everywhere, or
  // `here` for this project); the guard refuses an agent's `fm risk off`, so a braked agent can't lift its own brake.
  // On is fm's (removing the record lifts nothing).
  on('command.run', { command: 'fm-risk' }, async ($, e) => {
    const kind = e.origin?.kind
    if (kind !== 'composer' && kind !== 'bridge') return { text: 'Ward: /fm-risk works only when you type it.' }
    const [arg, where] = e.args.trim().split(/\s+/)
    const here = where === 'here'
    if (arg !== 'off' && arg !== 'on') return { text: 'Usage: /fm-risk off|on [here]' }
    if (arg === 'off') {
      const files = (await read($, view))?.risk_files
      const path = files?.[here ? 1 : 0]
      if (!path) return { text: 'Sampo has not told this pane where the record goes yet; try again after a turn.' }
      await $.fs.write(path, JSON.stringify({ on: true, at: new Date(await $.clock.now()).toISOString().slice(0, 19) + 'Z', via: kind }))
    }
    const r = await run($, ['risk', ...(arg === 'on' ? ['on'] : []), ...(here && arg === 'on' ? ['--here'] : [])])
    isDirty = true
    await refresh($)
    return { text: arg === 'off' ? `Risk budget off ${here ? 'for this project' : 'everywhere'} (by you). /fm-risk on restores it.` : `${r.stdout.trim() || r.stderr.trim()}`.split('\n').at(-1) ?? '' }
  })

  on('turn.start', async ($, e, next) => {
    // Claude Code 2.1.299: turn.start fires for subagents too (with agentId); their progress is the Subagents card's,
    // and resetting here would wipe the main turn's counts mid-turn (T-0944)
    if (e.agentId) return next(e)
    if (!turns.size) (turnStartedAt = await $.clock.now()), (treeTall = 0)
    turns.add(e.turnId)
    const a = (await read($, view))?.active
    turn = { tools: 0, edits: 0, add: 0, del: 0, agents: 0, stepsDone: a ? a.steps.filter(s => s.done).length : -1, task: a?.id ?? '' }
    await wake($)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    turns.delete(e.turnId)
    isDirty = true
    await update($, frame, n => n + 1) // T-0182 live: a reply drawn live redraws settled (●, its resting border)
    if (e.agentId) await update($, agents, list => list.map(a => (a.id === e.agentId ? { ...a, done: true } : a)))
    else {
      const fresh = await refresh($)
      const v = await read($, view)
      const a = v?.active
      const now = a && a.id === turn.task ? a.steps.filter(s => s.done).length : -1
      const step = turn.stepsDone >= 0 && now > turn.stepsDone && a ? `${a.id} step ${now}/${a.steps.length}` : ''
      const s: TurnSummary = { durationMs: e.durationMs, tools: turn.tools, edits: turn.edits, add: turn.add, del: turn.del, agents: turn.agents, step }
      await update($, summaries, list => [...list, s].slice(-40))
      // Ghost text for the obvious next move; never a suggestion that would answer a question put to the person.
      if (fresh && !e.isAborted && v?.project && !waiting(v) && (a || (v.queue ?? []).length))
        await $.prompt.suggest({ text: '/sampo:next' }).catch(() => undefined)
    }
    return next(e)
  })

  // T-1262: a grant is a person's to give. Sampo's PreToolUse hook asks for each lone fm ask; answered here as this
  // plugin's own ask, Claude Code puts it only to the dialog, a host's callback or a PermissionRequest hook: auto
  // mode's classifier, bypass mode and a saved rule for all of Bash can't settle it, and where nobody can be asked it
  // is refused. On a failure the verdict beneath stands: the classic ask (the grant still needs the dialog's record).
  on('tool.check', { tool: 'Bash' }, async ($, e, next) => {
    const r = await next(e)
    return r.decision === 'ask' && r.hook === 'PreToolUse' && /^(?:Foreman|Sampo) asks /.test(r.reason ?? '')
      ? { decision: 'ask', reason: r.reason }
      : r
  }).catch(($, e, next) => next(e)) // the verdict beneath: the classic ask, never an allow of its own

  on('tool.call', async ($, e, next) => {
    const id = e.tool_use_id
    let noteTimer: { cancel: () => void } | null = null
    const face = toolFace(String(e.tool), e, cfg.root)
    const at = await $.clock.now()
    starts.set(id, at)
    if (face.icon !== '⚠') ticks.push({ at, lane: laneOf(face.icon), what: face.target || face.verb }) // T-0909: the swim lanes (a question isn't work)
    while (ticks.length && (ticks.length > 2000 || at - ticks[0]!.at > 2 * TIMELINE_MS)) ticks.shift()
    running.set(id, { icon: face.icon, verb: face.verb, target: face.target, color: face.color, at, agent: !!e.agentId })
    const item: FeedItem = { id, agent: e.agentId ?? null, icon: face.icon, verb: face.verb, target: face.target, color: face.color, at }
    if (face.icon !== '⚠') feed.push(item) // T-1033: the band's tree
    while (feed.length > 300) feed.shift()
    try {
      if (face.icon === '◆') agentCalls.set(id, String((e as { description?: unknown }).description ?? ''))
      if (e.agentId) await noteSubagent($, e.agentId, `${face.verb} ${face.target}`.trim()).catch(() => undefined)
      else {
        turn.tools += 1
        if (face.add !== undefined) {
          turn.edits += 1
          turn.add += face.add
          turn.del += face.del ?? 0
        }
        if (face.icon === '◆') turn.agents += 1
      }
      await wake($)
      const note = e.tool === 'Bash' ? askNote(e.command) : null
      noteTimer = note ? $.clock.after(150, () => $.ui.notice(id, note)) : null // under the dialog, once it is open
      const r = await next(e)
      item.ok = r.deny === undefined && !r.isError
      const why = guardReason(r)
      if (why) $.ui.toast(`⛔ Ward: ${why}`, { timeoutMs: 8000 })
      else if (face.add !== undefined && r.deny === undefined && !r.isError) {
        const file = (e as { file_path?: unknown }).file_path // the real path; the files card shows it project-relative
        await noteEdit($, typeof file === 'string' ? file : face.target, face.add, face.del ?? 0)
      }
      const bg = (r.result as { backgroundTaskId?: unknown } | undefined)?.backgroundTaskId
      if (e.tool === 'Bash' && typeof bg === 'string' && !e.agentId) {
        const startedAt = await $.clock.now()
        const command = String(e.command).split('\n')[0]!.slice(0, 120)
        await noteShells($, list => [...list.filter(x => x.id !== bg), { id: bg, command, startedAt }])
      }
      const stopped = ['TaskStop', 'KillShell', 'KillBash'].includes(String(e.tool))
      if (stopped && r.deny === undefined && !r.isError) {
        const input = e as { task_id?: unknown; shell_id?: unknown }
        const gone = String(input.task_id ?? input.shell_id ?? '')
        await noteShells($, list => list.filter(x => x.id !== gone))
      }
      return r
    } finally {
      noteTimer?.cancel()
      const agent = agentOf.get(id)
      if (agent) await update($, agents, list => list.map(a => (a.id === agent ? { ...a, done: true } : a))).catch(() => undefined)
      const began = starts.get(id)
      if (began !== undefined) took.set(id, (await $.clock.now()) - began)
      if (took.size > 400) took.delete(took.keys().next().value!)
      starts.delete(id)
      item.end = await $.clock.now()
      if (item.ok === undefined) item.ok = false // review: a call that threw is not a ✓
      running.delete(id)
      agentCalls.delete(id)
      agentOf.delete(id)
      isDirty = true
    }
  })

  // T-1090 ('the this session has a few inlined/summarized things for what YOURE doing, and each subagent has its own'):
  // every model step's text, per subagent (agentId; none on the main loop), feeds its node in the live tree. A pure
  // tap: each chunk goes on unchanged, and a failure here never touches the stream.
  on('turn.step', async function* ($, e, next) {
    const key = e.agentId ?? ''
    const at = ++saidSeq // this step's entry (review: no clock round trip before the first chunk)
    let acc = ''
    let settled = false
    const note = (text: string, live: boolean) => {
      try {
        const line = narration(text)
        const list = (said.get(key) ?? []).filter(x => x.at !== at)
        if (line) list.push({ text: line, at, live })
        said.delete(key) // review: re-inserted last, so eviction takes the least recent node, never this session
        said.set(key, list.slice(-3))
        if (said.size > 40) said.delete([...said.keys()].find(k => k !== '')!) // this session's own words always stay
      } catch { /* the tree is a view; the stream is not ours to break */ }
    }
    const stream = next(e)
    try {
      for await (const ch of stream) {
        if (acc.length < 400 && (ch as { kind?: unknown }).kind === 'text') { // review: the line is cut at 200; stop there
          acc += String((ch as { text?: unknown }).text ?? '')
          note(acc, true)
        }
        yield ch
      }
      const r = await stream.result
      note(typeof r?.answer === 'string' && r.answer ? r.answer : acc, false)
      settled = true
      return r
    } finally {
      if (!settled) note(acc, false) // an abort or error: the line stops spinning
    }
  })

  // T-0137: Claude Code folds commands into one "Running 1 shell command…" line, which hid the live row; a group
  // holding a shell command unfolds, so each command is its own row here. Reads and searches stay folded.
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    // T-1040: a folded group is one branch of the turn's tree; an unfolded one is the rows it unfolds into
    if (e.props.isExpanded) return next(e)
    if (!e.props.calls.some(x => x.tool === 'Bash')) return branch($.ui.resolve(e), e, 'fm-group', await next(e), { at: 'body' })
    return next({ ...e, props: { ...e.props, isExpanded: true } })
  })

  // T-0198: economy mode, one session section after the engine's while a usage window is past its threshold.
  on('prompt.compose', async ($, e, next) => {
    const r = await next(e)
    const text = economyText((await $.session.usage().catch(() => null))?.rateLimits ?? [], weeklyLean)
    return text ? { ...r, sections: [...r.sections, { id: 'sampo-ui:economy', text, scope: 'session' }] } : r
  })

  // A background task's notification (idle, or folded into a running turn) names the task that ended.
  on('prompt.submit', async ($, e, next) => {
    const kind = e.origin?.kind
    if ((kind === 'composer' || kind === 'bridge') && (await read($, away)).length) await update($, away, () => [])
    if (kind === 'task-notification') {
      const ended = new Set([...e.text.matchAll(/<task-id>([^<]+)<\/task-id>/g)].map(m => m[1]!.trim()))
      await noteShells($, list => list.filter(x => !ended.has(x.id)))
    }
    return next(e)
  })

  // While a tool runs: an animated row. A Sampo bookkeeping command that succeeded: one quiet line. Else the engine's.
  // T-0141: under an edit row that already shows its diff, the engine's own result block ('⎿ Added 2 lines' and the
  // whole hunk again, on red and green backgrounds) is blanked; an error still draws the engine's
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    // T-0143: a shell row draws its output, file changes, commit and timeout itself; the engine's block repeated them
    const { Box } = $.ui.resolve(e)
    if (e.props.tool === 'Bash') return <Box key="fm-shell-result" />
    if (!e.props.isErrored && agentOut(e.props.tool, e.props.output)) return <Box key="fm-agent-result" /> // T-0148
    if (ownsResult(e.props.tool, !!e.props.isErrored, e.props.output)) return <Box key="fm-done-result" /> // T-1031: in the row
    if (e.props.isErrored || !editChanges(e.props.tool, e.props.output)) return branch($.ui.resolve(e), e, 'fm-result', await next(e), { at: 'body' }) // T-1040
    return <Box key="fm-edit-shown" />
  })
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    // T-1040: each call hangs on the turn's tree; a one-line call sits tight, a section gets a rail line above it
    const tree = (r: RenderElement, gap = true) => branch($.ui.resolve(e), e, 'fm-branch', r, { gap })
    const sub = e.props.tool === 'Bash' ? fmCommand((e.props.input as { command?: unknown } | null)?.command) : null
    if (sub && !e.props.isRunning && !e.props.isErrored && !e.props.isInterrupted) {
      const { Box, Text } = $.ui.resolve(e)
      return tree(
        <Box flexDirection="row" gap={1} key="fm-quiet">
          <Text color={hex(C.dim)}>⚙ fm {sub}</Text>
          <Text color={hex(mix(C.dim, C.ok, 0.6))} wrap="truncate-end">
            ✓ {lastLine(e.props.output)}
          </Text>
        </Box>, false)
    }
    if (e.props.tool === 'Bash' && !e.props.isRunning) return tree(shellRow($.ui.resolve(e), e))
    const quiet = ['Read', 'WebFetch', 'WebSearch', 'Grep', 'Glob'].includes(e.props.tool)
    if (quiet && !e.props.isRunning && !e.props.isErrored && !e.props.isInterrupted) {
      const face = toolFace(e.props.tool, e.props.input, cfg.root)
      const { Box, Text } = $.ui.resolve(e)
      const more = readSummary(e.props.tool, e.props.output)
      const kc = kindChip(`${face.icon} ${face.verb}`) // T-3701: the kind's chip in the Board's look
      return tree(
        <Box flexDirection="row" gap={1} key="fm-read">
          {kc ? <Box flexShrink={0}>{chip($.ui.resolve(e), 'fm-read-chip', kc.word, face.color)}</Box> : <Text color={hex(face.color)}>{face.icon}</Text>}
          <Text weight="bold" color={hex(mix(face.color, C.muted, 0.5))}>{face.verb.replace(/ing\b/, '')}</Text>
          <Text wrap="truncate-start" color={hex(0xd0d5dd)}>{face.target}</Text>
          {more ? <Text color={hex(C.muted)}>· {more}</Text> : null}
        </Box>, false)
    }
    const done = !e.props.isRunning && !e.props.isErrored && !e.props.isInterrupted
    const edited = done ? editRow($.ui.resolve(e), e) ?? agentRow($.ui.resolve(e), e) ?? toolRow($.ui.resolve(e), e) : null
    if (edited) return tree(edited)
    if (!e.props.isRunning || e.props.isErrored || e.props.isInterrupted) return branch($.ui.resolve(e), e, 'fm-branch', await next(e), { at: 'body' }) // an engine row opens with its own blank line: the rail runs past it, no ├─ on the blank
    const f = await read($, frame)
    const face = toolFace(e.props.tool, e.props.input, cfg.root)
    const since = starts.get(e.props.tool_use_id)
    const ms = since === undefined ? 0 : (await $.clock.now()) - since
    const agentId = agentOf.get(e.props.tool_use_id)
    const agent = agentId ? (await read($, agents)).find(a => a.id === agentId) : undefined
    const { Box, Text } = $.ui.resolve(e)
    const width = Math.max(30, (e.viewport?.columns ?? 100) - (treeOn(e) ? TREE_INDENT : 0)) // T-1040: beside the gutter
    // T-0124: a brainstorm running inside this call shows its progress right here in the chat
    const ideasRun = e.props.tool === 'Bash' && /^ideas\b/.test(fmCommand((e.props.input as { command?: unknown } | null)?.command) ?? '')
    const bs = ideasRun ? (await read($, view))?.brainstorm : null

    // T-1031 ('make the full chat UI match the panel look'): a running call is a section head like the pane's, a comet
    // running along its rule; its facts on the right
    const { head } = sectionHead($.ui.resolve(e), e, {
      key: 'fm-tool', color: face.color, mark: spin(f), label: `${face.icon} ${face.verb}`,
      subject: face.target.slice(0, Math.max(10, width - 48)), frame: f, width,
      meta: [...(face.delta ? [{ text: face.delta, color: C.edit }, { text: ' · ' }] : []),
        ...(agent ? [{ text: `${agent.tools} tools · ${agent.last.slice(0, 30)} · ` }] : []), { text: elapsed(ms) }],
    })
    const row = <Box key="fm-tool" flexDirection="column" width="100%" marginTop={gapTop(e)}>{head}</Box>
    if (!bs?.running) return tree(row)
    return tree(
      <Box flexDirection="column" key="fm-tool-ideas">
        {row}
        <Text color={hex(C.agent)} wrap="truncate-end">
          {'  '}✦ {bs.answers}/{bs.expected ?? '?'} answers · {bs.count} ideas so far{bs.ideas.length ? ` · ${bs.ideas.at(-1)}` : ''}
        </Text>
      </Box>)
  })

  // A background task's notification: one Sampo line (ctrl+o still shows the engine's full row).
  on('ui.render', { component: 'UserMessage' }, async ($, e, next) => {
    const origin = e.props.origin as { kind?: string; name?: string } | undefined
    const ours = /^The (?:sampo|foreman)-ui plugin sent a message:\n/ // how the model reads it; the row may carry that text
    if (((origin?.kind === 'plugin' && /^(?:sampo|foreman)-ui$/.test(origin.name ?? '')) || ours.test(e.props.text)) && !e.props.isExpanded) {
      // T-0143 live: our own resume prompt drew as a four-line grey block; one quiet line says it
      const { Box, Text } = $.ui.resolve(e)
      return branch($.ui.resolve(e), e, 'fm-branch', ( // T-1088: on the rail like every other row
        <Box flexDirection="row" gap={1} key="fm-resumed">
          <Text color={hex(C.accent)}>↻</Text>
          <Text color={hex(C.dim)} wrap="truncate-end">
            {clean(e.props.text.replace(ours, '').split('\n')[0] ?? '')}
          </Text>
        </Box>
      ))
    }
    // T-0182: 'my chats aren't [panelled] either': the person's own prompt is a panel in their colour
    if ((origin?.kind === 'composer' || origin?.kind === 'bridge') && e.props.text.length <= 9000 &&
        !/[\u0000-\u0008\u000b-\u001f\u007f]/.test(e.props.text)) {
      const { Box, Text } = $.ui.resolve(e)
      return (
        <Box flexDirection="column" width="100%" marginTop={1} key="fm-user">
          {sectionHead($.ui.resolve(e), e, { key: 'fm-user', color: C.accent2, label: '❯ You', width: e.viewport?.columns ?? 100 }).head}
          <Box key="fm-user-body" flexDirection="row" width="100%">
            <Box flexShrink={0} width={treeOn(e) ? TREE_INDENT : 2} />
            <Box flexDirection="column" flexGrow={1} flexShrink={1}>
              <Text wrap="wrap" color={hex(0xe6e9ef)}>{e.props.text}</Text>
            </Box>
            {treeOn(e) ? railBox($.ui.resolve(e), 'fm-user-rail', 0) : null /* T-1040: the turn's tree starts here */}
          </Box>
        </Box>
      )
    }
    if (e.props.origin?.kind !== 'task-notification' || e.props.isExpanded) return next(e)
    const t = e.props.task ?? {}
    const status = t.status ?? 'completed'
    const color = status === 'completed' ? C.ok : status === 'failed' ? C.err : C.warn
    const { Box, Text } = $.ui.resolve(e)
    return branch($.ui.resolve(e), e, 'fm-branch', ( // T-1088
      <Box flexDirection="row" gap={1} key="fm-note">
        <Text color={hex(color)}>{status === 'completed' ? '✓' : status === 'failed' ? '✗' : '■'}</Text>
        <Text color={hex(C.dim)}>background {t.type ?? 'task'} {status}</Text>
        <Text wrap="truncate-end">{e.props.text.split('\n')[0]}</Text>
        {t.durationMs ? <Text color={hex(C.dim)}>· {elapsed(t.durationMs)}</Text> : null}
      </Box>
    ))
  })

  // T-0143: the working line is Sampo's ('get rid of … the "Seasoning" thing … and have it be a foreman thing'): an
  // animated mark, the task and step, what the turn is doing, how long it has run. The desktop keeps its own row.
  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    if (e.surface !== 'terminal') return next(e)
    if (treeDrawn && !e.props.message) { // T-1039: the band's tree root says the task, step and time; a message still shows
      const { Box } = $.ui.resolve(e)
      return <Box key="fm-working" />
    }
    const a = (await read($, view))?.active
    const cur = a?.steps.find(s => s.current)
    const f = await read($, frame)
    const ms = turnStartedAt ? (await $.clock.now()) - turnStartedAt : 0
    const doing = e.props.message ?? DOING[e.props.mode] ?? 'working'
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="row" gap={1} key="fm-working">
        <Text color={hex(C.accent)}>{spin(f)}</Text>
        <Text weight="bold" color={hex(C.accent)}>
          {a ? a.id : 'Sampo'}
        </Text>
        {a && cur && (
          <Text wrap="truncate-end">
            <Text color={hex(C.dim)}>{`step ${a.steps.filter(s => s.done).length + 1}/${a.steps.length} `}</Text>
            {cur.text}
          </Text>
        )}
        <Text color={hex(C.dim)}>{`· ${doing} · ${elapsed(ms)}`}</Text>
      </Box>
    )
  })

  // T-0143: a reply opens with a Sampo mark (animated on the newest reply while the turn runs) and Sampo's report
  // lines (Changed:, ✓/✗, ⚑ Captured, ◆ Decided, Next:, ⚠ Needs you) are coloured like its rows; the rest stays the
  // engine's markdown ('just your normal text output looks weird with everything else').
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    const text = e.props.text
    // a Markdown element takes ≤10000 characters, tab and newline its only controls: beyond that the engine's own draws
    const at = e.props.isFirstOfReply ? { gap: true } : { at: 'body' as const } // T-1040: a reply is a branch of the turn
    if (e.surface !== 'terminal') return next(e)
    if (text.length > 9000 || /[\u0000-\u0008\u000b-\u001f\u007f]/.test(text)) return branch($.ui.resolve(e), e, 'fm-reply-branch', await next(e), { at: 'body' })
    if (turns.size && e.props.isFirstOfReply && !seenReplies.has(text)) {
      if (seenReplies.size > 400) seenReplies.clear()
      seenReplies.add(text)
      newestReply = text
    }
    const live = turns.size > 0 && e.props.isFirstOfReply && text === newestReply
    const f = live ? await read($, frame) : 0
    const blocks = replyBlocks(text)
    // T-1031: a reply is a section like the pane's: its first block opens with the mark, a bold label and a rule in the
    // accent (a comet along it while the reply is live); the blocks under it
    const { Box, Text, Markdown } = $.ui.resolve(e)
    return branch($.ui.resolve(e), e, 'fm-reply-branch',
      <Box flexDirection="column" width="100%" marginTop={e.props.isFirstOfReply ? gapTop(e) : 0} key="fm-reply">
        {e.props.isFirstOfReply &&
          sectionHead($.ui.resolve(e), e, { key: 'fm-reply', color: C.accent, mark: live ? spin(f) : '●', markKey: 'fm-reply-mark',
            label: 'Claude', frame: live ? f : null }).head}
        <Box flexDirection="column" flexShrink={1} paddingLeft={2}>
          {blocks.map((b, i) =>
            b.kind === 'gap' ? (
              <Box key={`gap-${i}`}>
                <Text> </Text>
              </Box>
            ) : b.kind === 'md' ? (
              <Markdown key={`md-${i}`} text={b.text} />
            ) : (
              <Text key={`ln-${i}`} wrap="wrap">
                {b.parts.map((p, j) => (
                  <Text key={j} weight={p.bold ? 'bold' : 'regular'} {...(p.color !== undefined ? { color: hex(p.color) } : {})}>
                    {p.text}
                  </Text>
                ))}
              </Text>
            ),
          )}
        </Box>
      </Box>, at)
  })

  // The line closing a turn says what the turn did; T-1040: the tip of the turn's tree.
  on('ui.render', { component: 'TurnDuration' }, async ($, e, next) => {
    const s = (await read($, summaries)).find(x => x.durationMs === e.props.durationMs)
    // T-1040 live: the engine's own line lays out under the tip, not beside it; in the tree a turn without tools gets ours
    if ((!s || !s.tools) && !treeOn(e)) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return branch($.ui.resolve(e), e, 'fm-turn-tip',
      <Box flexDirection="row" gap={1} key="fm-turn">
        <Text dimColor>✻ worked {elapsed(e.props.durationMs)}</Text>
        {s?.tools ? <Text color={hex(C.dim)}>· {summaryText(s)}</Text> : null}
      </Box>, { at: 'tip' })
  })

  // The footer: a pointer when something waits on the person (the band says the autonomy in words).
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    const v = await read($, view)
    if (e.props.isDraft || !waiting(v)) return next(e)
    const shown = (await $.ui.panes().catch(() => [])).some(p => p.id === PANE && p.isShown) // /fm would close it
    return next({ ...e, props: { ...e.props, tail: shown ? '⚠ Sampo needs you: see the pane' : '⚠ Sampo needs you: /fm' } }) // the engine puts the separator before it
  })

  // T-3163: 'a few other things arent [rendered like the rest of the mod text] either': the five rows no hook drew. Each
  // keeps its words; the engine keeps its behaviour (ctrl+b is its own key, answers go through its dialog). Text with
  // escapes or past 9000 characters stays the engine's, under the Sampo mark. A missing prop reads as '' (a throw would
  // trip the breaker).
  const str = (t: unknown) => String(t ?? '')
  const plain = (t: unknown) => str(t).length <= 9000 && !/[\u0000-\u0008\u000b-\u001f\u007f]/.test(str(t))

  // the background hint under a running call: the key in the accent, the words quiet, on the call's rail
  on('ui.render', { component: 'ToolProgress' }, async ($, e, next) => {
    const hint = str(e.props.hint)
    if (e.surface !== 'terminal' || e.props.kind !== 'background_hint' || !hint.trim() || !plain(hint)) return next(e)
    const m = /^\((.+?) to (.+)\)$/.exec(hint)
    const { Box, Text } = $.ui.resolve(e)
    return branch($.ui.resolve(e), e, 'fm-hint-branch', (
      <Box flexDirection="row" gap={1} paddingLeft={2} key="fm-hint">
        <Text color={hex(C.dim)}>↧</Text>
        {m ? <Text weight="bold" color={hex(C.accent)}>{m[1]}</Text> : null}
        <Text color={hex(C.dim)} wrap="truncate-end">{m ? `to ${m[2]}` : hint}</Text>
      </Box>), { at: 'body' })
  })

  // a slash command's output (/cost, /model …): the command as a section head, its markdown under it as the engine
  // draws it (an error line in the error colour)
  on('ui.render', { component: 'CommandOutput' }, async ($, e, next) => {
    const { command, isErrored } = e.props
    const args = str(e.props.args), text = str(e.props.text)
    if (e.surface !== 'terminal' || !plain(args)) return next(e)
    const color = isErrored ? C.err : C.accent
    const { head, rest } = sectionHead($.ui.resolve(e), e, { key: 'fm-cmd', color, mark: '▸', label: `/${command}`,
      subject: args || undefined, meta: isErrored ? [{ text: 'failed', color: C.err }] : [] })
    const { Box, Text, Markdown } = $.ui.resolve(e)
    return branch($.ui.resolve(e), e, 'fm-cmd-branch', (
      <Box flexDirection="column" marginTop={gapTop(e)} key="fm-cmd">
        {head}
        {rest ? <Box paddingLeft={2}><Text color={hex(LIGHT)} wrap="wrap">{rest}</Text></Box> : null}
        {!text.trim() ? null /* no output: the head alone */
          : !plain(text) ? await next(e) /* its own ⎿ gutter: no padding, every column for width-laid output (/context) */
          : <Box flexDirection="column" paddingLeft={2}>
            {isErrored ? <Text color={hex(C.err)} wrap="wrap">{text}</Text> : <Markdown key="fm-cmd-md" text={text} />}
          </Box>}
      </Box>), { gap: true })
  })

  // a startup notice ('Using Opus … · /model'): a quiet mark, the words, the command to change it in the accent
  on('ui.render', { component: 'InfoNotice' }, async ($, e, next) => {
    const text = str(e.props.text)
    if (e.surface !== 'terminal' || !text.trim() || !plain(text)) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="row" gap={1} key="fm-notice">
        <Text color={hex(C.accent2)}>ℹ</Text>
        <Text color={hex(C.muted)} wrap="wrap">{text}</Text>
        {e.props.command ? <Text color={hex(C.dim)}>·</Text> : null}
        {e.props.command ? <Text color={hex(C.accent)}>{str(e.props.command)}</Text> : null}
      </Box>
    )
  })

  // the footer's mode badges: each mode bold in its colour (a dangerous one in the error colour); the desktop lays its own
  on('ui.render', { component: 'SessionMode' }, async ($, e, next) => {
    const modes: string[] = e.props.modes ?? []
    if (e.surface !== 'terminal' || !modes.length) return next(e)
    const { Text } = $.ui.resolve(e)
    return (
      <Text key="fm-modes">{/* the labels only, as the engine's own node: the footer lays the separator before it */}
        {modes.map((m, i) => (
          <Text key={i}>
            {i ? <Text color={hex(C.dim)}>{' & '}</Text> : null}
            <Text weight="bold" color={hex(/bypass|danger|skip/i.test(m) ? C.err : C.accent2)}>{m}</Text>
          </Text>
        ))}
      </Text>
    )
  })

  // a question for the person: a Sampo head over the engine's own dialog (never replaced: it maps the answers back)
  on('ui.render', { component: 'AskUserQuestion' }, async ($, e, next) => {
    if (e.surface !== 'terminal') return next(e)
    const qs: { header?: string; question?: string }[] = e.props.questions ?? []
    const subject = qs.map(q => q.header).filter(Boolean).join(' · ') || qs[0]?.question
    const { Box, Text } = $.ui.resolve(e)
    // the dialog refuses sizing props and Rasters around it, so this head is one plain row, not sectionHead's
    return (
      <Box flexDirection="column" key="fm-ask">
        <Box flexDirection="row" gap={1} key="fm-ask-head">
          <Text weight="bold" color={hex(C.warn)}>? Claude asks</Text>
          {subject ? <Text color={hex(LIGHT)} wrap="truncate-end">{subject}</Text> : null}
          <Text color={hex(C.dim)}>{`${qs.length} question${qs.length === 1 ? '' : 's'}`}</Text>
        </Box>
        {await next(e)}
      </Box>
    )
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const v = await read($, view)
    if (e.props.hasSurvey || !v?.project) return next(e)
    const a = v.active
    const asks = v.approvals ?? []
    const plans = (v.queue ?? []).filter(q => q.waits === 'plan approval')
    const bgs = await read($, shells)
    const doneSince = await read($, away)
    const paused = v.health?.paused_hooks ?? [] // T-0087: hooks the breaker paused after failing in a row
    const idle = !a && !asks.length && !plans.length && !(v.queue ?? []).length && !(v.inbox_total ?? 0) && !paused.length
    // T-1033: the live tree, while work runs and for two minutes after the last call
    const treeAt = await $.clock.now()
    const treeSubs = await read($, agents)
    const lastCall = feed.at(-1)
    treeDrawn = false // until a tree is drawn below
    const treeLive = !!lastCall && (e.props.isWorking || treeSubs.some(x => !x.done) || treeAt - (lastCall.end ?? lastCall.at) < TREE_MS)
    if (idle && !bgs.length && !doneSince.length && !v.mode?.trust && !treeLive) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const working = e.props.isWorking
    const f = working || bgs.length ? await read($, frame) : null
    const subsBg = bgs.length ? (await read($, agents)).filter(x => !x.done).length : 0
    const nowMs = bgs.length ? await $.clock.now() : 0
    const c = await read($, ctx)
    const m = v.mode
    const done = a ? a.steps.filter(s => s.done).length : 0
    const cur = a?.steps.find(s => s.current)
    const narrow = e.props.bodyColumns < 90 // beside a docked pane: one column of single lines, details dropped
    const width = narrow ? Math.max(6, Math.min(12, e.props.bodyColumns - 28)) : Math.max(10, Math.min(28, e.props.bodyColumns - 70))
    const queued = v.queue_total ?? (v.queue ?? []).length
    const inboxN = v.inbox_total ?? (v.inbox ?? []).length
    const todayDone = v.today_done ?? 0
    const inFlight = flowWords(v) // T-3664: the work in lanes, which the queue leaves out
    const upNext = (v.queue ?? [])[0] ?? (v.inbox ?? [])[0]
    const usual = a ? v.typical?.[`${a.type}/${a.tier}`] : undefined
    const bandNow = await $.clock.now()
    const cut = 'truncate-end' as const
    // T-1039 ('sticky so the text doesn't scroll the chat'): one height for the turn, padded, with a blank row under it
    const tree = (rows: number, sticky = true) => {
      treeDrawn = sticky
      const els = liveTree($.ui.resolve(e), e, { feed, subs: treeSubs, live: working, now: treeAt, frame: f, said, worktrees: v.worktrees ?? [],
        width: e.props.bodyColumns, rows: sticky ? Math.max(1, rows - 2) : rows, turnMs: turnStartedAt ? treeAt - turnStartedAt : 0,
        task: a ? `${a.id} · step ${Math.min(a.steps.length, done + 1)}/${a.steps.length}${cur ? ` ${cur.text}` : ''}` : 'no task in focus' })
      if (!sticky) return <Box key="fm-band-tree" flexDirection="column">{els}</Box>
      treeTall = Math.min(Math.max(1, rows - 2), Math.max(treeTall, els.length, 5))
      const { Raster } = $.ui.resolve(e)
      return (
        <Box key="fm-band-tree" flexDirection="column">
          {/* T-1090 ('theres no bar/gap seperating the this session from the chat UI'): a fading rule over the tree */}
          {e.surface === 'terminal'
            ? <Raster key="tree-rule" columns={Math.max(10, e.props.bodyColumns - 1)} rows={1} cells={ruleCells(Math.max(10, e.props.bodyColumns - 1), C.accent)} />
            : null}
          {els}
          {Array.from({ length: Math.max(0, treeTall - els.length) }, (_, i) => <Text key={`tree-pad-${i}`}> </Text>)}
          <Box key="tree-gap" height={1} />
        </Box>
      )
    }
    // T-0989: with the pane shown it is the one panel: its attention row has what the band would say. T-1033: the band
    // then carries only what the pane can't, the live tree of the session and its subagents, under the chat
    if ((await $.ui.panes().catch(() => [])).some(p => p.id === PANE && p.isShown)) // idle: the root alone, one summary line
      return treeLive ? tree(Math.max(3, e.props.maxRows)) : lastCall ? tree(1, false) : next(e)
    // the card's own rows, so the tree under it never pushes the band past maxRows (review)
    const cardRows = 2 + (a ? 2 : 1) + (queued > 0 || inboxN > 0 ? 1 : 0) + (doneSince.length ? 1 : 0) + (bgs.length ? 1 : 0) +
      (!working && v.next ? 1 : 0) + (paused.length ? 2 : 0) + asks.length + Math.min(2, plans.length) + 1
    const treeRows = e.props.maxRows - cardRows
    const sl = signalLine(v) // T-0472: quiet unless something is amber or red

    // Beside a docked pane the band is narrow: each row is one Text that cuts at its end, never mid-word per span.
    const head = narrow ? (
      <Box flexDirection="column" key="fm-band-head">
        {a ? (
          <Text wrap={cut}>
            <Text weight="bold" color={typeColor(a.type)}>
              {a.type}
            </Text>{' '}
            <Text color={hex(C.dim)}>{sizeWord(a.tier)}</Text>{' '}
            <Text weight="bold" color={hex(C.accent)}>
              {a.id}
            </Text>{' '}
            <Text weight="bold">{a.title}</Text>
          </Text>
        ) : (
          <Text weight="bold" color={hex(C.accent)} wrap={cut}>
            Sampo · nothing active
          </Text>
        )}
        {a && a.steps.length > 0 && (
          <Box flexDirection="row" gap={1}>
            {working && <Text color={hex(C.accent)}>{spin(f ?? 0)}</Text>}
            {meter($.ui.resolve(e), e, 'fm-band-bar', width, done / a.steps.length, C.steel, f)}
            <Text wrap={cut}>
              {/* T-1154: the current step's own number ('1/3' beside step 2's text read as step 1) */}
              <Text weight="bold">{`step ${Math.min(a.steps.length, done + 1)}/${a.steps.length}`}</Text>{' '}
              {cur?.text ?? ''}
            </Text>
          </Box>
        )}
      </Box>
    ) : (
      <Box flexDirection="column" key="fm-band-head">
        <Box flexDirection="row" justifyContent="space-between">
          <Box flexDirection="row" gap={1} flexShrink={1}>
            {a ? (
              <Box flexDirection="row" gap={1} flexShrink={1}>
                {/* the title gives way first: the id never wraps (seen live: 'T-014' then '1' on the next row) */}
                <Box flexDirection="row" gap={1} flexShrink={0} key="fm-band-id">
                  <Text weight="bold" color={typeColor(a.type)}>
                    {a.type}
                  </Text>
                  <Text color={hex(C.dim)}>{sizeWord(a.tier)}</Text>
                  <Text weight="bold" color={hex(C.accent)}>
                    {a.id}
                  </Text>
                </Box>
                <Text weight="bold" wrap={cut}>
                  {a.title}
                </Text>
              </Box>
            ) : (
              <Text weight="bold" color={hex(C.accent)} wrap={cut}>
                Sampo · nothing active
              </Text>
            )}
          </Box>
          <Box flexDirection="row" gap={1} flexShrink={0}>
            {c && <Text color={hex(C.dim)}>context</Text>}
            {c && meter($.ui.resolve(e), e, 'fm-band-ctx', 6, c.percent / 100, c.percent >= CHECKPOINT_AT ? C.err : c.percent >= 60 ? C.warn : C.ok, null)}
            {c && <Text color={hex(C.dim)}>{Math.round(c.percent)}%</Text>}
            {m && <Text color={hex(m.autonomy === 'full' ? C.accent2 : C.dim)}>{m.autonomy === 'full' ? 'full auto' : 'standard'}</Text>}
            {m && !m.drive && <Text color={hex(C.warn)}>drive off</Text>}
            {m?.trust && <Text color={hex(C.warn)}>trust on</Text>}
            {m?.risk_off && <Text color={hex(C.warn)}>risk off</Text>}
            {v.loop?.on && <Text color={hex(C.agent)}>∞ round {v.loop.round}</Text> /* T-1057: the forever loop */}
            {sl && <Text color={hex(sl.level === 'red' ? C.err : C.warn)} wrap={cut}>{sl.level === 'red' ? '●' : '◐'} {sl.text}</Text>}
          </Box>
        </Box>
        {a && (
          <Box flexDirection="row" gap={1}>
            {working && <Text color={hex(C.accent)}>{spin(f ?? 0)}</Text>}
            {/* T-0909: a mini stage rail, the pane's in small */}
            {miniRail($.ui.resolve(e), e, a.stages.length, a.stages.indexOf(a.stage), working ? f : null)}
            <Text color={hex(C.dim)}>{a.stage}</Text>
            {a.steps.length > 0 && meter($.ui.resolve(e), e, 'fm-band-bar', width, done / a.steps.length, C.steel, f)}
            {a.steps.length > 0 && <Text weight="bold">{`step ${Math.min(a.steps.length, done + 1)}/${a.steps.length}`}</Text>}
            {cur && <Text wrap={cut}>{cur.text}</Text>}
            {a.audits.need > 0 && (
              <Text color={hex(a.audits.done >= a.audits.need ? C.ok : C.dim)}>
                · audits {a.audits.done}/{a.audits.need}
              </Text>
            )}
            {typeof a.on_task_s === 'number' && (
              <Text color={hex(C.dim)} wrap={cut}>
                · {elapsed(a.on_task_s * 1000 + Math.max(0, bandNow - lastFull))} on it
                {usual ? ` · usually ${about(usual)}` : ''}
              </Text>
            )}
          </Box>
        )}
      </Box>
    )

    return (
      <Box flexDirection="column" borderStyle="round" borderColor={hex(working ? pulse(C.accent, f) : C.track)} paddingX={1} key="fm-band">
        {head}
        {treeLive && treeRows >= 3 ? tree(treeRows) : null}
        {(queued > 0 || inboxN > 0 || inFlight.length > 0) && (
          // T-3701: one dense line in the Board's language: a chip, the counts as pills, today's meter, what is next
          <Box flexDirection="row" gap={1} key="fm-band-queue-row" height={1} overflow="hidden">
            <Box flexShrink={0}>{chip($.ui.resolve(e), 'fm-band-chip', narrow ? '⊖' : 'SAMPO', C.accent)}</Box>
            {([[`✓ ${todayDone} done`, C.ok], ...inFlight.map(w => [w, / building$/.test(w) ? C.accent2 : / to merge$/.test(w) ? C.accent : C.ok]),
              [`${queued} queued`, C.muted], [`${inboxN} inbox`, v.inbox_yours ? C.accent : C.muted]] as [string, number][]).map(([w, c], i) => (
              <Box key={`band-pill-${i}`} flexShrink={0}>{pill($.ui.resolve(e), `band-pill-${i}`, w, c)}</Box>
            ))}
            {!narrow && <Text color={hex(C.dim)}>today</Text>}
            {!narrow && meter($.ui.resolve(e), e, 'fm-band-queue', 10, todayDone / Math.max(1, todayDone + queued + inboxN + (a ? 1 : 0)), C.accent2, null)}
            {upNext && !narrow ? <Text color={hex(C.dim)} wrap={cut}>{`next ${upNext.id} ${upNext.title}`}</Text> : null}
          </Box>
        )}
        {doneSince.length > 0 && (
          <Text color={hex(C.ok)} wrap={cut} key="fm-band-away">
            ✔ since your last message: {doneSince.length} done · {doneSince.slice(-6).join(' ')}
          </Text>
        )}
        {bgs.length > 0 && (
          <Box flexDirection="row" gap={1} key="fm-band-wait">
            <Text color={hex(C.accent2)}>{spin(f ?? 0)}</Text>
            <Text color={hex(C.accent2)} wrap={cut}>
              ◷ waiting on {bgs.length} background shell{bgs.length === 1 ? '' : 's'}
              {subsBg ? ` and ${subsBg} subagent${subsBg === 1 ? '' : 's'}` : ''}
            </Text>
            <Text wrap={cut}>{bgs[0]!.command}</Text>
            <Text color={hex(C.dim)} wrap={cut}>
              {elapsed(nowMs - bgs[0]!.startedAt)}
              {working ? '' : ' · Claude picks up when they finish'}
            </Text>
          </Box>
        )}
        {!working && v.next && !(a && v.next.startsWith(`${a.id} step `)) && ( // T-1154: not the step the head shows
          <Text color={hex(C.dim)} wrap={cut}>
            → {humanNext(v.next)}
          </Text>
        )}
        {paused.length > 0 && (
          <Text color={hex(C.warn)} wrap={cut} key="fm-band-paused">
            ⚠ Sampo's {paused.join(', ')} hook{paused.length === 1 ? ' is' : 's are'} paused: it failed 3 times in a row
            and runs again within 10 min (fm doctor)
          </Text>
        )}
        {asks.map(x => (
          <Text color={hex(C.warn)} wrap={cut} key={`ask-${x.task}`}>
            ⚠ Needs you: {x.task} {x.allow.join(', ')} — {x.why}
          </Text>
        ))}
        {plans.slice(0, 2).map(q => (
          <Box flexDirection="row" gap={1} key={`plan-${q.id}`}>
            <Text color={hex(C.warn)} wrap={cut}>
              ⚠ {q.id} ({sizeWord(q.tier)}) plan awaits approval: {q.title}
            </Text>
            {/* consent is given in the pane, where the plan it approves is shown */}
            <Button key={`review-${q.id}`} label="review" hotkey="v" plain onPress={() => void openPane($, true)} />
          </Box>
        ))}
        <Box flexDirection="row" gap={2}>
          <Button key="open" label="dashboard" hotkey="f" plain onPress={() => void openPane($, true)} />
          {!working && (
            <Button
              key="next"
              label="next"
              hotkey="n"
              plain
              onPress={() => void $.prompt.submit({ text: '/sampo:next', asUser: true })}
            />
          )}
        </Box>
      </Box>
    )
  })

  // T-0989, T-1000, T-1002: one panel that fills the dock exactly. The head and the task read first, then the one thing
  // that needs you; then sections, each under a titled divider (a blank row above it on a tall dock): Activity (one plot
  // of the session's tool calls by kind, with what runs now at each lane's end), Up next, Sessions, History (it takes the
  // rows left) and Today (closes per day beside four tank gauges). Few words per row; every number has a picture.
  // Rasters that move are registered for the 30 fps blit while work is live.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const ui = $.ui.resolve(e)
    const { Box, Button, Input, Raster, Text } = ui
    const v = await read($, view)
    const err = await read($, error)
    if (!v?.project) {
      return (
        <Box flexDirection="column">
          <Text color={hex(C.dim)}>Not a Sampo project here.{err ? ` (${err})` : ''}</Text>
          <Text color={hex(C.dim)}>Ask Claude to set it up, or run /sampo:intake with a request.</Text>
        </Box>
      )
    }
    if (e.props.bodyColumns < PANE_MIN) // T-1679: below the layout's floor one line says what it needs, never a spill
      return <Text color={hex(C.dim)} wrap="truncate-end">{`Sampo: ${PANE_MIN}+ columns`}</Text>
    const live = turns.size > 0 || starts.size > 0
    const clockNow = await $.clock.now()
    const moving = live && cfg.motion !== 'off' // T-0912: motion off draws work still
    const f = moving || clockNow < flashUntil ? await read($, frame) : null // a fading step glow redraws too
    const beatNow = await read($, beat) // T-1437: read with the mascot off too, so the poll's repaint redraws times
    const idleBeat = live || cfg.mascot === 'off' ? 0 : beatNow
    const term = e.surface === 'terminal'
    if (term) motion.clear() // this drawing registers what moves
    // the rasters' time: the blit's clock in full motion (so a redraw never jumps), the frame in calm, none when off
    const ft = !moving ? null : cfg.motion === 'full' ? frameAt(clockNow) : f
    const blit = term && cfg.motion === 'full' ? motion : undefined
    const a = v.active
    const m = v.mode
    const W = Math.max(PANE_MIN - 2, e.props.bodyColumns - 2) // inside the panel's padding
    const H = e.props.scroll?.bodyRows ?? 40
    const dim = hex(C.dim)
    const soft = hex(mix(C.dim, C.track, 0.3))
    const muted = hex(C.muted) // T-1026: secondary text that still reads
    const subs = await read($, agents)
    const bgShells = await read($, shells)
    const fb = bgShells.length ? await read($, frame) : 0
    const isOn = await read($, sound)
    const cx = await read($, ctx)
    const shownPlan = await read($, reviewing)
    const doneSince = await read($, away)
    const queue = v.queue ?? []
    const queueN = v.queue_total ?? queue.length // T-3664: the list is the first 20
    const inbox = v.inbox ?? []
    const inboxN = v.inbox_total ?? inbox.length
    const ln = v.lanes ?? { building: [], merging: [], closing: [] }
    const checks = v.checks
    const stageIdx = a ? a.stages.indexOf(a.stage) : -1
    const usual = a ? v.typical?.[`${a.type}/${a.tier}`] : undefined
    const onMin = a && typeof a.on_task_s === 'number' ? (a.on_task_s * 1000 + Math.max(0, clockNow - lastFull)) / 60000 : null
    const passed = checks ? checks.results.filter(r => !r.exit).length : 0
    const failing = !!checks && passed < checks.results.length
    const asks = v.approvals ?? []
    const plans = queue.flatMap(q => (q.plan ? [{ ...q, plan: q.plan }] : []))
    const nowCalls = [...running].filter(([, x]) => !x.agent)
    const asking = nowCalls.find(([, x]) => x.icon === '⚠')
    const days = v.done_days ?? []
    const paused = v.health?.paused_hooks ?? []
    const since = (ms: number) => (ms < 3600000 ? `${Math.max(1, Math.round(ms / 60000))}m ago` : hoursAgo(ms / 3600000))
    const raster = (key: string, cols: number, cellsOf: string, twin: string, color: number) =>
      term ? <Raster key={key} columns={cols} rows={1} cells={cellsOf} /> : <Text key={key} color={hex(color)}>{twin}</Text>

    // ── the one thing that needs you
    const callo: { v: { color: number; text: string; plan?: string; title?: string } | null } = { v: null } // T-3701: the board's callout
    const attention = (() => {
      const row = (color: number, text: string, lead?: RenderElement | null, tail?: RenderElement | null) => {
        callo.v = { color, text }
        return (
          <Box key="fm-pane-attention" flexDirection="row" gap={1} height={1} overflow="hidden" flexShrink={0}>
            {lead}
            <Text color={hex(color)} wrap="truncate-end">
              {text}
            </Text>
            {tail}
          </Box>
        )
      }
      if (err) return row(C.err, `✗ fm: ${err}`)
      if (paused.length) // Sampo's own checks are off: before anything else (T-0087)
        return row(C.warn, `⚠ ${paused.join(', ')} hook${paused.length === 1 ? ' is' : 's are'} paused after failing 3 times in a row (fm doctor)`)
      if (asking) return row(C.warn, `⚠ waiting for your answer, ${elapsed(clockNow - asking[1].at)}`)
      if (asks.length) {
        const x = asks[0]!
        return row(C.warn, `⚠ needs you: ${x.task} ${x.allow.join(', ')} — ${x.why}${asks.length > 1 ? ` (+${asks.length - 1} more)` : ''}`)
      }
      if (plans.length) {
        const q = plans[0]!
        const el = row(
          C.warn,
          `⚠ ${q.id} plan awaits your yes${a ? `: ${q.title}` : ''}${plans.length > 1 ? ` (+${plans.length - 1} more)` : ''}`,
          null,
          <Box key="attention-plan" flexDirection="row" gap={1} flexShrink={0}>
            <Button key={`att-approve-${q.id}`} label="✓ approve" plain onPress={() => act($, ['task', 'set', q.id, 'approved=true'], `✓ ${q.id} plan approved`)} />
            {a && (
              <Button key={`review-${q.id}`} label={shownPlan === q.id ? 'hide' : 'show'} plain dimColor
                onPress={() => update($, reviewing, x => (x === q.id ? null : q.id))} />
            )}
          </Box>,
        )
        callo.v = { ...callo.v!, plan: q.id, title: q.title }
        return el
      }
      if (closing && clockNow - closing.at < CLOSING_MS) {
        const c = closing
        const sweep = (t: number) => Math.min(1, Math.max(0, (t - c.at) / SWEEP_MS))
        if (blit) blit.set('closed-sweep', { columns: 8, rows: 1, draw: fr => progressCells(8, sweep(fr * FRAME_MS), mix(C.ok, C.accent2, 0.6), C.ok, null) })
        const facts = [
          c.proved !== null ? `${c.proved} criteri${c.proved === 1 ? 'on' : 'a'} proven` : '',
          c.minutes !== null ? elapsed(c.minutes * 60000) : '',
          c.usual !== undefined ? `usually ${about(c.usual)}` : '',
        ].filter(Boolean)
        return row(C.steel, `✔ ${c.id} done${closeCard(c)}${facts.length ? ` · ${facts.join(' · ')}` : ''}${c.title ? ` · ${c.title}` : ''}`,
          <Box key="attention-sweep" flexShrink={0}>{meter(ui, e, 'closed-sweep', 8, cfg.motion === 'off' ? 1 : sweep(clockNow), C.ok, null)}</Box>)
      }
      if (failing) return row(C.err, `✗ gates failing: ${checks!.results.filter(r => r.exit).map(r => r.cmd).join(', ')}`)
      const caps = v.budget?.halved || v.budget?.subagents_paused
      if (caps) return row(C.warn, `◐ spend caps halved: ${caps}${v.budget?.subagents_paused ? ', subagents wait' : ''}`)
      const rv = (v.revisit ?? [])[0]
      if (rv) return row(C.warn, `◆ revisit: ${rv.decision} (${rv.why})`)
      if (doneSince.length) return row(C.steel, `✔ since your last message: ${doneSince.length} done (${doneSince.slice(-6).join(' ')})`)
      const sl = signalLine(v) // T-0472: usage ahead of pace, tasks blocked …: the band's glyph, here while the pane is open
      if (sl) return row(sl.level === 'red' ? C.err : C.warn, `${sl.level === 'red' ? '●' : '◐'} ${sl.text}`)
      if (v.genesis && v.genesis.kind !== 'done') return row(C.dim, `⌂ genesis gen ${v.genesis.gen} · ${v.genesis.stage}: ${v.genesis.action}`) // T-2118
      return row(C.dim, '✓ nothing needs you')
    })()
    // T-3701: the board page: the Board's top bar over its panels; the controls as key caps, as many as the width holds
    const page = await read($, pageAtom)
    const keycap = (k: string) => <Text key="cap" weight="bold" color={hex(C.ink)} backgroundColor={hex(mix(C.bg, C.line, 0.95))}>{` ${k} `}</Text>
    // a control as a key cap and its words: the click on a plain Button holding them, the key on a hotkey Button kept
    // 0 wide beside it (a plain Button with a hotkey draws `p:` before its words, which a key cap replaces)
    const capped = (key: string, k: string, word: string, press: () => unknown, color: number, words = true, short?: string) => (
      <Box key={`${key}-ctl`} flexDirection="row" flexShrink={0}>
        <Box key={`${key}-hot`} width={0} height={1} overflow="hidden"><Button key={`${key}-key`} label={word} hotkey={k} plain onPress={press} /></Box>
        <Button key={key} label={word} plain onPress={press}>
          {keycap(k)}
          {words ? <Text key="word" color={hex(color)}>{` ${short ?? word}`}</Text> : null}
        </Button>
      </Box>
    )
    // the word on a key cap is short; the Button's label says the whole action
    const ctl: { key: string; k: string; word: string; short?: string; board?: false; press: () => unknown }[] = [
      { key: 'fm-page', k: 'p', word: page === 'board' ? 'details' : 'board', press: async () => { // T-3701: the store keeps it
        const to = (await read($, pageAtom)) === 'board' ? 'details' : 'board'
        await update($, pageAtom, () => to)
        await $.store.set('page', to).catch(() => undefined)
      } },
      { key: 'next', k: 'n', word: 'next', press: () => void $.prompt.submit({ text: '/sampo:next', asUser: true }) },
      { key: 'drive', k: 'd', word: m?.drive ? 'turn drive off' : 'turn drive on', short: m?.drive ? 'drive off' : 'drive on', press: () => act($, ['drive', m?.drive ? 'off' : 'on'], `drive ${m?.drive ? 'off' : 'on'}`) },
      { key: 'scrub-back', k: 'b', word: '◂ back', board: false, press: () => update($, scrub, x => x + 1) },
      { key: 'scrub-live', k: 'l', word: 'live', board: false, press: () => update($, scrub, () => 0) },
      { key: 'autonomy', k: 'u', word: m?.autonomy === 'full' ? 'switch to standard' : 'switch to full auto', short: m?.autonomy === 'full' ? 'standard' : 'full auto', press: () => {
        const level = m?.autonomy === 'full' ? 'standard' : 'full'
        return act($, ['autonomy', level], `autonomy ${level}`)
      } },
      { key: 'sound', k: 'm', word: isOn ? 'mute' : 'unmute', press: async () => {
        await update($, sound, x => !x)
        await $.store.set('sound', await read($, sound))
      } },
      { key: 'refresh', k: 'r', word: 'refresh', press: () => refresh($) },
    ]
    // each a key cap and its words while they fit, then the cap alone (the label still names it): never cut mid-word
    const ctlShown = ctl.filter(c => page !== 'board' || c.board !== false) // the time scrub steps through the details' history
    let room = W - (ctlShown.length - 1) * 2 - ctlShown.length * 3
    const worded = new Set(ctlShown.filter(c => (room -= (c.short ?? c.word).length + 1) >= 0).map(c => c.key))
    const controls = (
      <Box flexDirection="row" gap={2} key="fm-pane-controls" height={1} overflow="hidden" flexShrink={0}>
        {ctlShown.map(c => capped(c.key, c.k, c.word, c.press, C.muted, worded.has(c.key), c.short))}
      </Box>
    )
    if (page === 'board') {
      // the top bar, in the panels' chrome: ┌ SAMPO, the project and how it runs, the work as counts ┐; the task in
      // focus as the NOW strip; one amber callout when something needs you, its actions as key caps; └ … ┘
      const bracket = hex(C.bracket)
      const modeLine = [m?.autonomy === 'full' ? 'full auto' : 'standard', ...(m && !m.drive ? ['drive off'] : []), ...(m?.trust ? ['trust on'] : []), ...(m?.risk_off ? ['risk off'] : []),
        ...(v.loop?.on ? [`∞ round ${v.loop.round}`] : [])].join(' · ')
      // the counts, the ones that matter most first (a narrow bar drops from the end): numbers bright, words dim
      const stats = ([[v.today_done ?? 0, 'done', C.ok], [ln.building.length, 'building', C.accent2], [ln.merging.length, 'to merge', C.accent],
        [queueN, 'queued', C.ink], [inboxN, 'inbox', v.inbox_yours ? C.accent : C.ink], [v.blocked_total ?? 0, 'blocked', C.warn],
        [ln.closing.length, 'to close', C.ok]] as [number, string, number][]).filter(([n, w]) => n || ['done', 'queued', 'inbox'].includes(w))
      const proj = fitText(v.project ?? '', Math.max(4, Math.floor(W / 4)))
      // ┌ (2), the chip (9), the project (1 + its), the mode (2 + its), a rule between spaces, the counts, ┐ (2)
      const base = 2 + 9 + 1 + proj.length, mode = 2 + modeLine.length
      const statW = (s: typeof stats) => s.reduce((t, [n, w], i) => t + String(n).length + 1 + w.length + (i ? 2 : 0), 0)
      let shownStats = stats
      while (shownStats.length && base + 3 + statW(shownStats) + 2 > W) shownStats = shownStats.slice(0, -1)
      const showMode = base + mode + 3 + statW(shownStats) + 2 <= W
      const rule = Math.max(1, W - base - (showMode ? mode : 0) - 2 - statW(shownStats) - 2)
      const need = callo.v && (callo.v.color === C.warn || callo.v.color === C.err) ? callo.v : null
      const stepsDone = a ? a.steps.filter(s => s.done).length : 0
      const mw = W >= 60 ? 10 : 6 // the NOW strip's meter
      const timeWord = onMin !== null ? elapsed(onMin * 60000) : ''
      const nowRow = (last: boolean) => (
        <Box key="head-now" flexDirection="row" height={1} overflow="hidden" flexShrink={0}>
          <Text color={bracket}>{last ? '└ ' : '  '}</Text>
          {a ? (
            <Box key="now" flexDirection="row" flexGrow={1} flexShrink={1} overflow="hidden">
              <Box key="now-head" flexDirection="row" flexShrink={0}>
                <Text color={dim}>NOW </Text>
                {chip(ui, 'now-kind-chip', a.type, typeNum(a.type))}
                <Text weight="bold" color={hex(C.accent)}>{` ${a.id} `}</Text>
              </Box>
              <Box key="now-title" flexGrow={1} flexShrink={1} overflow="hidden"><Text weight="bold" color={hex(C.ink)} wrap="truncate-end">{a.title}</Text></Box>
              <Box key="now-tail" flexDirection="row" flexShrink={0}>
                {W >= 72 ? <Text color={dim}>{`  ${a.stage}`}</Text> : null}
                <Text color={hex(C.edit)}>{`  ${'━'.repeat(Math.round((mw * stepsDone) / Math.max(1, a.steps.length)))}`}</Text>
                <Text color={hex(mix(C.line, C.dim, 0.4))}>{'─'.repeat(mw - Math.round((mw * stepsDone) / Math.max(1, a.steps.length)))}</Text>
                <Text color={dim}>{` ${stepsDone}/${a.steps.length}${timeWord ? ` · ${timeWord}` : ''}`}</Text>
              </Box>
            </Box>
          ) : (
            <Box key="now" flexGrow={1} flexShrink={1} overflow="hidden">
              <Text color={dim} wrap="truncate-end">{`NOW  ${live ? 'working, no task in focus' : 'nothing in focus'}${v.next ? `  · next: ${humanNext(v.next)}` : ''}`}</Text>
            </Box>
          )}
          <Text color={bracket}>{last ? ' ┘' : '  '}</Text>
        </Box>
      )
      const callout = need && (
        <Box key="head-callout" flexDirection="row" height={1} overflow="hidden" flexShrink={0}>
          <Text color={bracket}>└ </Text>
          <Box key="callout" flexDirection="row" flexGrow={1} flexShrink={1} overflow="hidden" backgroundColor={hex(mix(C.bg, need.color, 0.16))}>
            <Box key="callout-text" flexGrow={1} flexShrink={1} overflow="hidden">
              <Text color={hex(need.color)} wrap="truncate-end">{` ${need.plan
                ? (W - 30 >= `⚠ ${need.plan} plan awaits your yes: ${need.title ?? ''}`.length ? `⚠ ${need.plan} plan awaits your yes: ${need.title ?? ''}` : `⚠ ${need.plan} plan, your yes: ${need.title ?? ''}`)
                : need.text.replace(/^[⚠✗◐●◆]\s*/, '⚠ ')}`}</Text>
            </Box>
            {need.plan ? (
              <Box key="callout-keys" flexDirection="row" gap={2} flexShrink={0}>
                {capped(`att-approve-${need.plan}`, 'a', 'approve', () => act($, ['task', 'set', need.plan!, 'approved=true'], `✓ ${need.plan} plan approved`), need.color)}
                {capped(`review-${need.plan}`, 's', 'show', async () => {
                  await update($, reviewing, () => need.plan!) // the plan is read on the details page
                  await update($, pageAtom, () => 'details')
                }, need.color)}
                <Text key="callout-end"> </Text>
              </Box>
            ) : null}
          </Box>
          <Text color={bracket}> ┘</Text>
        </Box>
      )
      const headRows = callout ? 3 : 2
      const boardHead = (
        <Box key="fm-pane-head" flexDirection="column" height={headRows} overflow="hidden" flexShrink={0}>
          <Box key="head-bar" flexDirection="row" height={1} overflow="hidden" flexShrink={0}>
            <Text color={bracket}>┌ </Text>
            <Text key="head-mark" weight="bold" color={hex(C.on)} backgroundColor={hex(C.accent)}>{` ${paneMark(moving, f ?? 0)} SAMPO `}</Text>
            <Text key="head-project" color={muted}>{` ${proj}`}</Text>
            {showMode ? <Text key="head-mode" color={dim}>{`  ${modeLine}`}</Text> : null}
            <Text key="head-rule" color={hex(mix(C.line, C.dim, 0.3))}>{` ${'─'.repeat(rule)} `}</Text>
            {shownStats.map(([n, w, color], i) => (
              <Text key={`head-stat-${w}`}>
                <Text weight="bold" color={hex(color)}>{`${i ? '  ' : ''}${n}`}</Text>
                <Text color={dim}>{` ${w}`}</Text>
              </Text>
            ))}
            <Text color={bracket}> ┐</Text>
          </Box>
          {nowRow(!callout)}
          {callout || null}
        </Box>
      )
      // T-3701: the live page Board's nine panels and SESSIONS, each a picture, in its order (when the dock can't hold them all, the
      // ones the state makes most relevant first), a grid of 1, 2 or 3 columns by the pane's width; only the panels in
      // the window draw, and only a moving one redraws
      const train = v.train
      const flows = v.flows ?? []
      const bd = v.board ?? {}
      const cpuNowB = v.cpu ? v.cpu.now ?? v.cpu.sessions.reduce((t, s) => t + (s.pct.at(-1) ?? 0), 0) : 0
      const spin = (s: string) => [C.accent2, C.edit, C.accent, C.agent, C.ok, C.warn][[...s].reduce((t, ch) => t + ch.charCodeAt(0), 0) % 6]!
      const churn = await read($, files)
      // three columns on a very wide dock, two from 84 (a panel 41 wide: a docked pane is often ~90), else one
      const ncol = W >= 200 ? 3 : W >= 84 ? 2 : 1, gapX = 2
      const ORDER = ['lanes', 'merge', 'cpu', 'loop', 'library', 'models', 'sampo', 'state', 'sessions', 'tests'] as const
      const layout = gridRows(ORDER.length, ncol, W, gapX), nrow = layout.length // a short last row widens: no empty slot
      // a picture's rows: as many as let every row of panels fit the dock (with the head and the controls), 12 to 24:
      // under 12 a picture says too little, so a short dock scrolls
      // T-4008: a dock too short for every row shows whole rows at the top: as many as fit with 12-row pictures, the
      // pictures grown to fill the dock and the lines left over above the grid, so no panel is cut at the dock's foot
      const fitAll = Math.floor((H - headRows - 2) / nrow) - 3 // every row and the controls
      const fit = fitAll >= 12 ? nrow : Math.max(1, Math.floor((H - headRows) / 15))
      const PH = fit === nrow ? Math.min(24, fitAll) : Math.max(12, Math.min(24, Math.floor((H - headRows) / fit) - 3))
      const gapHead = fit === nrow ? 1 : Math.max(1, H - headRows - fit * (PH + 3) + 1)
      const used = headRows + gapHead + nrow * (PH + 2) + (nrow - 1)
      const hot = new Set<string>([...(ln.merging.length || train?.running ? ['merge'] : []), ...(ln.building.length ? ['lanes'] : []),
        ...(a ? ['loop'] : []), ...(flows.some(f => f.hot) ? ['models'] : [])])
      const order = used + 2 <= H ? [...ORDER] : [...ORDER.filter(k => hot.has(k)), ...ORDER.filter(k => !hot.has(k))]
      const off = e.props.scroll?.offset ?? 0
      let pw = 0 // the width of the panel being drawn: its row's share
      // T-4008: a note's first line is what its panel lacks; the foot doesn't say it again
      const note = (...lines: string[]) => Object.assign(() => notePicture(pw, PH, lines.filter(Boolean)), { says: lines[0] })
      const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`
      type Panel = { name: string; what: string; label: string; caption: string; stat: string; pic: () => Picture; twin: string[] }
      const panel = (k: (typeof ORDER)[number]): Panel => {
        if (k === 'lanes') {
          const L = [...ln.building.map(l => ({ l, state: 'building' })), ...ln.merging.map(l => ({ l, state: 'waiting' }))]
          const held = L.reduce((t, x) => t + (x.l.files?.length ?? 0), 0)
          return { name: 'LANES', what: 'builders and the files they hold', label: plural(L.length, 'lane').toUpperCase(),
            caption: L.length ? `building ${ln.building.length} · waiting ${ln.merging.length}` : '', stat: held ? `${held} files held` : '',
            pic: () => lanesPicture(pw, PH, L.map(({ l, state }) => ({ id: l.id, title: l.title, color: spin(l.id), done: l.steps_done, total: l.steps_total,
              files: l.files ?? [], state, next: l.id === ln.merging[0]?.id }))),
            twin: L.map(({ l, state }) => `◉ ${l.id} ${state} ${l.steps_done}/${l.steps_total} · ${l.title}`) }
        }
        if (k === 'merge') {
          const landed = train?.landed ?? ln.closing.map(l => l.id)
          const d = { waiting: ln.merging.map(l => ({ id: l.id, title: l.title, color: typeNum(l.type), frac: l.steps_total ? l.steps_done / l.steps_total : 1 })),
            landed: landed.map((id, i) => ({ id, at: train?.landed_at?.[i] ?? '' })), landed24: train?.landed_24h ?? landed.length, running: !!train?.running, branch: train?.branch }
          return { name: 'MERGE TRAIN', what: 'finished lanes into one build', label: train?.running ? 'TRAIN RUNNING' : (train?.branch || 'build').toUpperCase(),
            caption: `${train?.landed_24h ?? ln.closing.length} landed in 24 h${train?.parked ? ` · ${train.parked} parked` : ''}`,
            stat: `${ln.merging.length} waiting`, pic: () => mergePicture(pw, PH, d),
            twin: [`${ln.merging.length} waiting → gates → build · ${train?.landed_24h ?? ln.closing.length} landed`, ...ln.merging.map(l => `▣ ${l.id} ${l.title}`)] }
        }
        if (k === 'cpu') {
          // by kind of work, as the web Board's (the board cache reads /proc each refresh); else this project's sessions
          const bc = bd.cpu, cpu = v.cpu
          const len = cpu ? Math.max(0, ...cpu.sessions.map(s => s.pct.length)) : 0
          const span = cpu ? Math.min(30, Math.round((len * cpu.step_s) / 60)) : 0 // the minutes the samples cover
          const kin: CpuIn | null = bc?.now
            ? { cores: bc.cores, goal: bc.goal_pct, kinds: bc.kinds, now: bc.now, minutes: bc.minutes }
            : cpu ? { cores: cpu.cores, goal: cpu.goal, kinds: ['claude'], now: { claude: (cpuNowB / 100) * cpu.cores, busy: (cpuNowB / 100) * cpu.cores },
              minutes: Array.from({ length: span }, (_, m) => { // a row a minute, from the sample each minute falls in
                const i = len - 1 - Math.floor(((span - 1 - m) * 60) / Math.max(1, cpu.step_s))
                return [m * 60, (cpu.sessions.reduce((t, s) => t + (s.pct[s.pct.length - len + i] ?? 0), 0) / 100) * cpu.cores]
              }) }
            : null
          if (!kin) return { name: 'CPU', what: 'the machine by kind of work', label: 'GOAL 10%', caption: 'no CPU reading yet', stat: '',
            pic: note('no CPU reading yet', 'sampo cpu samples the machine'), twin: ['no CPU reading yet'] }
          const busy = kin.now.busy ?? 0, share = kin.cores ? (100 * busy) / kin.cores : 0
          const peak = Math.max(0, ...kin.minutes.map(r => r.slice(1).reduce((t, x) => t + x, 0)))
          const mins = kin.minutes.length ? Math.min(30, Math.round((kin.minutes.at(-1)![0]! - kin.minutes[0]![0]!) / 60) + 1) : 0
          return { name: 'CPU', what: 'the machine by kind of work', label: share > kin.goal ? 'OVER GOAL' : `GOAL ${kin.goal}%`,
            caption: mins ? `peak ${peak.toFixed(1)} cores in ${mins} min` : 'the first minute',
            stat: `${share.toFixed(1)}%`,
            pic: () => cpuPicture(pw, PH, kin),
            twin: [`${busy.toFixed(1)} of ${kin.cores} cores busy · goal ${kin.goal}%`, ...kin.kinds.map(k => `${k} ${(kin.now[k] ?? 0).toFixed(1)}`)] }
        }
        if (k === 'loop') {
          if (!a) return { name: 'THE LOOP', what: 'your words into a brief', label: 'IDLE', caption: 'no task in focus', stat: '',
            pic: note('no task in focus', v.next ? `next: ${humanNext(v.next)}` : ''), twin: ['no task in focus'] }
          const top = [...churn].sort((x, y) => y.add + y.del - (x.add + x.del)).slice(0, 10)
          const met = a.criteria.filter(c => c.checked).length
          return { name: 'THE LOOP', what: 'your words into a brief', label: a.stage.toUpperCase(),
            caption: `${a.id} · ${met} of ${plural(a.criteria.length, 'criterion').replace(/criterions$/, 'criteria')} met`,
            stat: a.steps.length ? `step ${Math.min(a.steps.length, a.steps.filter(s => s.done).length + 1)}/${a.steps.length}` : '',
            pic: () => taskPicture(pw, PH, { id: a.id, stage: a.stage, stages: a.stages, title: a.title, words: a.words ?? a.title,
              criteria: a.criteria.map(c => ({ text: c.text.split(' :: ')[0]!, ok: c.checked })),
              steps: a.steps.map(s => ({ text: s.text, done: s.done, current: s.current })), files: top.map(x => ({ add: x.add, del: x.del })),
              done: a.steps.filter(s => s.done).length, total: a.steps.length, time: onMin !== null ? elapsed(onMin * 60000) : '' }),
            twin: [`${a.id} · ${met}/${a.criteria.length} criteria · ${a.steps.filter(s => s.done).length}/${a.steps.length} steps`] }
        }
        if (k === 'library') {
          const counts = bd.counts, hourly = bd.library?.hourly ?? []
          const total = counts ? counts.cards + counts.ui + counts.moons + counts.calling : 0
          return { name: 'LIBRARY', what: 'cards and moons kept', label: `${total.toLocaleString('en')} KEPT`,
            caption: counts ? `cards ${counts.cards} · ui ${counts.ui} · moons ${counts.moons}` : "the library isn't read yet",
            stat: hourly.length ? `${hourly.at(-1)} / h` : '', pic: counts ? () => libraryPicture(pw, PH, { counts, hourly }) : note("the library isn't read yet"),
            twin: [counts ? `${total} kept · ${hourly.at(-1) ?? 0} this hour` : "the library isn't read yet"] }
        }
        if (k === 'models') {
          const calls = flows.reduce((t, f) => t + f.n, 0), models = new Set(flows.map(f => f.to)).size
          return { name: 'MODELS', what: 'who calls which model', label: `${Math.round(calls / 5)} CALLS / MIN`,
            caption: `${plural(calls, 'call')} in 5 min · ${plural(models, 'model')}`, stat: flows.some(f => f.hot) ? 'calling now' : '',
            pic: () => modelsPicture(pw, PH, { links: flows }), twin: flows.map(f => `${f.from} → ${f.to} · ${f.n}`) }
        }
        if (k === 'sampo') {
          const cmds = bd.commands ?? [], last = cmds.at(-1)
          return { name: 'SAMPO', what: 'commands and the queue', label: `${queueN} QUEUED`,
            caption: last ? `$ ${last.cmd}` : 'no sampo commands seen yet', stat: cmds.length ? `${cmds.filter(c => c.ok).length}/${cmds.length} ok` : '',
            pic: () => sampoPicture(pw, PH, { cmds: cmds.map(c => ({ cmd: c.cmd, ok: c.ok })), queued: queueN,
              queue: queue.map(q => ({ id: q.id, status: q.waits ? 'waits' : q.status, kind: `${q.type}/${q.tier}`, title: q.title })) }),
            twin: queue.slice(0, 6).map(q => `${q.id} ${q.status} · ${q.title}`) }
        }
        if (k === 'state') {
          const st = bd.state
          return { name: 'STATE', what: 'the files Sampo keeps', label: 'STATE/',
            caption: st ? `newest ${st.newest || '—'}` : "the state folder isn't read yet",
            stat: st?.doc ? `latest ${st.doc.kind}` : '', pic: st ? () => statePicture(pw, PH, st) : note("the state folder isn't read yet"),
            twin: st?.doc ? [`${st.doc.kind}: ${st.doc.title}`] : ['no decision yet'] }
        }
        if (k === 'sessions') {
          const fl = (v.fleet ?? []).filter(x => !x.error && x.session !== '-')
          const newest = Math.max(0, ...fl.map(x => Date.parse(x.seen ?? '') || 0))
          const SC = [C.accent2, C.edit, C.accent, C.agent, C.ok, C.warn]
          const colorOf = (pr: string) => (pr === v.project ? C.accent2 : SC[[...pr].reduce((t, ch) => t + ch.charCodeAt(0), 0) % SC.length]!)
          const idleN = fl.filter(x => newest - (Date.parse(x.seen ?? '') || 0) > 120000).length
          const projects = new Set(fl.map(x => x.remote ?? x.project ?? '')).size
          return { name: 'SESSIONS', what: 'every live session, any project', label: plural(fl.length, 'session').toUpperCase(),
            caption: fl.length ? `${plural(projects, 'project')} · ${idleN} idle` : 'no live sessions',
            stat: fl.length ? `context ${Math.round(fl.reduce((t, x) => t + (x.context_pct ?? 0), 0) / fl.length)}% avg` : '',
            pic: () => sessionsPicture(pw, PH, { sessions: fl.map(x => {
              const pr = x.remote ?? x.project ?? '—'
              return { id: x.session, project: pr, color: colorOf(pr), task: x.task ?? '', model: (x.model ?? '').toLowerCase(),
                pct: x.context_pct ?? null, idle: newest - (Date.parse(x.seen ?? '') || 0) > 120000 } // idle: 2 min behind the newest
            }) }),
            twin: fl.map(x => `${x.project ?? '—'} ${x.task ?? 'no task'} · ${x.model ?? '?'} · ${x.context_pct ?? '?'}%`) }
        }
        const ts = bd.tests, by = (s: string) => ts?.modules.filter(m => m.state === s).length ?? 0
        return { name: 'TESTS', what: 'every module, cached or run', label: plural(ts?.modules.length ?? 0, 'module').toUpperCase(),
          caption: ts ? `${by('fail')} failing · ${by('running')} running` : 'no test run seen yet',
          stat: ts?.total ? `${ts.done}/${ts.total} passed` : '', pic: ts ? () => testsPicture(pw, PH, { modules: ts.modules, source: ts.source }) : note('no test run seen yet'),
          twin: [ts ? `${by('fail')} failing · ${by('running')} running of ${ts.modules.length}` : 'no test run seen yet'] }
      }
      const grid: RenderElement[] = []
      let at = 0
      for (let r = 0; r < nrow; r++) {
        const y = headRows + gapHead + r * (PH + 3) // the row's first line in the tree: under the top bar and its gap
        const shown = y + PH + 2 > off && y < off + H
        const widths = layout[r]!
        grid.push(
          <Box key={`panel-row-${r}`} flexDirection="row" gap={gapX} flexShrink={0} marginTop={r ? 1 : 0}>
            {widths.map(w => {
              const k = order[at++]!
              pw = w
              const n = ORDER.indexOf(k) + 1, P = panel(k), color = panelAccent(n), key = `panel-${k}`
              let body: RenderElement
              if (!shown) body = <Box key={`${key}-pic`} height={PH} flexShrink={0} /> // off screen: nothing drawn
              else if (term) {
                const p = P.pic()
                // a moving part (packets, a pulse) redraws only while it moves and this session's clocks run, ≤10 fps
                if (p.live && blit && ft !== null) blit.set(`${key}-pic`, { columns: pw, rows: PH, draw: fr => p.live!((fr * FRAME_MS) / 1000) })
                body = <Raster key={`${key}-pic`} columns={pw} rows={PH} cells={p.live ? p.live(ft !== null ? (ft * FRAME_MS) / 1000 : 0) : p.cells} />
              } else body = (
                <Box key={`${key}-pic`} flexDirection="column" height={PH} overflow="hidden">
                  {P.twin.slice(0, PH).map((s, j) => <Text key={`twin-${j}`} color={j ? muted : hex(color)} wrap="truncate-end">{s}</Text>)}
                </Box>
              )
              return (
                <Box key={key} flexDirection="column" width={pw} flexShrink={0}>
                  {panelHead(ui, e, { key, n, name: P.name, what: P.what, label: P.label, color, width: pw })}
                  {body}
                  {panelFoot(ui, e, { key, caption: (P.pic as { says?: string }).says === P.caption ? '' : P.caption, stat: P.stat, color, width: pw })}
                </Box>
              )
            })}
          </Box>,
        )
      }
      return (
        <Box flexDirection="column" key="fm-pane" paddingX={1}>
          {boardHead}
          <Box key="gap-head" height={gapHead} flexShrink={0} />
          {grid}
          <Box key="gap-controls" height={Math.max(1, H - used - 1)} flexShrink={0} />
          {controls}
        </Box>
      )
    }

    // ── up next: plans waiting for a yes first, the queue, the inbox, a line to capture into it
    const capture = captureBox($, e)
    // T-3701: a table as the Board's SAMPO panel draws the queue: ID, a status pill, the kind and size, the title
    const narrowQ = W < 44 // no kind column below that: the title keeps room
    const idW = Math.max(6, ...[...ln.building, ...queue, ...inbox].map(x => x.id.length))
    const qRow = (key: string, id: string, idColor: number, status: [string, number], it: { type: string; tier: string }, title: RenderElement, tail: RenderElement[] = []) => (
      <Box flexDirection="row" gap={1} key={key} height={1} overflow="hidden">
        <Box flexShrink={0} width={idW}><Text color={hex(idColor)}>{id}</Text></Box>
        <Box flexShrink={0}>{pill(ui, `${key}-pill`, status[0], status[1], 8)}</Box>
        {narrowQ ? null : (
          <Box flexShrink={0} width={7}>
            <Text color={typeColor(it.type)}>{`${({ PERFORMANCE: 'PERF', SECURITY: 'SEC', RESEARCH: 'RSRCH', FEATURE: 'FEAT' } as Record<string, string>)[it.type] ?? it.type.slice(0, 5)}`.padEnd(6)}</Text>
          </Box>
        )}
        {title}
        {tail}
      </Box>
    )
    const queueRows: RenderElement[] = [
      <Box key="q-head" flexDirection="row" gap={1} height={1} overflow="hidden">
        <Box flexShrink={0} width={idW}><Text weight="bold" color={dim}>ID</Text></Box>
        <Box flexShrink={0} width={10}><Text weight="bold" color={dim}>STATUS</Text></Box>
        {narrowQ ? null : <Box flexShrink={0} width={7}><Text weight="bold" color={dim}>KIND</Text></Box>}
        <Text weight="bold" color={dim} wrap="truncate-end">TITLE</Text>
      </Box>,
      // T-3664: what is moving first: builders at work (the MERGE panel has the lanes waiting on a merge or a close)
      ...ln.building.map(l => qRow(`b-${l.id}`, l.id, C.accent2, ['building', C.accent2], l,
        <Text key="title" wrap="truncate-end">{l.title}</Text>,
        l.steps_total > 0 ? [<Box key="steps" flexShrink={0}><Text color={dim}>{`${l.steps_done}/${l.steps_total}`}</Text></Box>] : [])),
      ...[...queue].sort((x, y) => Number(!!y.plan) - Number(!!x.plan)).map((q, i) => qRow(`q-${q.id}`, q.id, q.waits ? C.warn : C.ink,
        q.plan ? ['plan', C.warn] : q.waits ? ['waits', C.warn] : i === 0 ? ['next', C.accent] : ['queued', C.dim], q,
        <Text key="title" wrap="truncate-end" color={q.waits ? hex(C.warn) : undefined}>{q.title}</Text>,
        q.plan ? [<Button key={`approve-${q.id}`} label="✓ approve" plain onPress={() => act($, ['task', 'set', q.id, 'approved=true'], `✓ ${q.id} plan approved`)} />] : [])),
      ...inbox.map(it => {
        const yours = it.source === 'user' || it.source === 'steer'
        return qRow(`in-${it.id}`, it.id, C.muted, yours ? ['yours', C.accent] : (it.age_days ?? 0) >= 7 ? ['stale', C.warn] : ['inbox', C.muted], it,
          <Text key="title" wrap="truncate-end" color={hex(mix(C.dim, C.ink, 0.7))}>{it.title}</Text>, [
            <Button key={`start-${it.id}`} label="start" plain dimColor
              onPress={() => void $.prompt.submit({ text: `Plan and start ${it.id} (/sampo:intake).`, asUser: true })} />,
            <Button key={`drop-${it.id}`} label="drop" plain dimColor
              onPress={() => act($, ['task', 'drop', it.id, 'dropped from the Sampo pane'], `${it.id} dropped`)} />,
          ])
      }),
      ...(inboxN > inbox.length ? [<Text key="in-more" color={dim}>{`… ${inboxN - inbox.length} more in the inbox`}</Text>] : []),
      ...(capture ? [capture] : []), // last: a crowded dock cuts it before a plan waiting for a yes
    ]
    if (queueRows.length === 1 || (queueRows.length === 2 && capture)) queueRows.shift() // a header over nothing: none

    // ── other sessions here, each with its context, and one steer for all (T-0711: one alone is this one)
    const fleet = v.fleet ?? []
    const pctColor = (p: number) => (p >= CHECKPOINT_AT ? C.err : p >= 60 ? C.warn : C.ok)
    const sessionRows: RenderElement[] = fleet.some(s => s.remote || s.error) || fleet.length > 1
      ? [
          ...fleet.slice(0, 4).map((s, i) =>
            s.error ? (
              <Text key={`more-fleet-${i}`} color={hex(C.warn)} wrap="truncate-end">! {s.error}</Text>
            ) : (
              <Box key={`more-fleet-${i}`} flexDirection="row" gap={1}>
                <Box flexShrink={0}>
                  <Text color={hex(s.remote ? C.accent2 : C.agent)}>{(s.remote ? s.remote.toUpperCase() : s.session.slice(0, 8)).padEnd(9)}</Text>
                </Box>
                <Box flexShrink={0}>
                  <Text color={hex(s.context_pct === undefined || s.context_pct === null ? C.dim : pctColor(s.context_pct))}>
                    {`${String(s.context_pct ?? '?').padStart(3)}%`}
                  </Text>
                </Box>
                <Text wrap="truncate-end">
                  {` ${s.task ?? 'idle'}`}
                  <Text color={muted}>{`${s.title ? ` ${s.title}` : ''}${s.mail ? `  ✉${s.mail}` : ''}${s.project && s.project !== v.project ? `  ${s.project}` : ''}`}</Text>
                </Text>
              </Box>
            )),
          ...(e.surface !== 'mobile'
            ? [<Input key="steer" placeholder="↯ steer every session… (Enter)" submitLabel="Steer" onSubmit={text => void steer($, text)} />]
            : []),
        ]
      : []

    // ── history: what happened, newest first, one short line each (the command and its output stay in the ledger);
    // T-1026: from fm ui's structured history when it has one: the id, the event in its colour, the task's title
    const markColor = (mark: string) =>
      '✗✖'.includes(mark) ? C.err : '✓✔■'.includes(mark) ? C.ok : '◆◇'.includes(mark) ? C.agent : '!?≈'.includes(mark) ? C.warn : C.accent2
    const recent = [...(v.recent ?? [])].reverse()
    const hist = v.history ?? []
    const back = Math.min(await read($, scrub), Math.max(0, hist.length - 1)) // T-1081
    const shown = hist.slice(0, hist.length - back)
    const asOf = back ? [<Text key="as-of" color={hex(C.warn)}>{`as of ${shown.at(-1)?.time || '?'} (${back} back) · press l for live`}</Text>] : []
    const historyRows: RenderElement[] = shown.length ? [...asOf, ...[...shown].reverse().map((ev, i) => (
      <Box key={`r-${i}`} flexDirection="row" gap={1} height={1} overflow="hidden">
        <Box flexShrink={0}>
          <Text color={muted}>{ev.time || '     '}</Text>
        </Box>
        <Box flexShrink={0} width={1}>
          <Text color={hex(markColor(ev.mark))}>{ev.mark}</Text>
        </Box>
        {ev.task ? (
          <Box flexShrink={0}>
            <Text color={hex(C.accent)}>{ev.task}</Text>
          </Box>
        ) : null}
        <Box flexShrink={0}>
          <Text color={hex('✗✖✓✔■'.includes(ev.mark) ? markColor(ev.mark) : 0xd0d5dd)}>{ev.text}</Text>
        </Box>
        <Text wrap="truncate-end" color={muted}>{ev.title ? ` ${ev.title}` : ''}</Text>
      </Box>
    ))] : recent.map((line, i) => {
      const [, time, rest] = /^\s*(\d\d:\d\d)\s+(.*)$/.exec(line) ?? [null, '', line.trim()]
      const ev = /^([✓✗≈]) (T-\d+) (step \d+|criterion \d+|check): /.exec(rest)
      const text = ev
        ? `${ev[2]} ${ev[3]} ${ev[1] === '✓' ? 'passed' : ev[1] === '✗' ? 'failed' : 'inconclusive'}`
        : rest.slice(2).replace(/^(T-\d+ audit [\w-]+): .*$/, '$1')
      const mark = rest[0] ?? ''
      const color = '✗✖'.includes(mark) ? C.err : '✓✔■'.includes(mark) ? C.ok : '◆◇'.includes(mark) ? C.agent : '!?≈'.includes(mark) ? C.warn : C.dim
      return (
        <Box key={`r-${i}`} flexDirection="row" gap={2}>
          <Box flexShrink={0}>
            <Text color={soft}>{time || '     '}</Text>
          </Box>
          <Box flexShrink={0}>
            <Text color={hex(color)}>{mark}</Text>
          </Box>
          <Text color={hex(fade(Math.max(0, recent.length - 1 - i), recent.length, 0xc8ccd4))} wrap="truncate-end">
            {text}
          </Text>
        </Box>
      )
    })

    // ── the task block's parts, so its height is known before the rows are shared out
    const planQ = plans.find(q => q.id === shownPlan) ?? (a ? undefined : plans[0])
    const extras: RenderElement[] = !a ? [] : [
      // T-0711 the parts fm task split made; T-0228 the debugging ledger, the oracle, a batch; T-0261 runs that proved nothing
      ...(a.children ?? []).map(k => (
        <Text key={`kid-${k.id}`} wrap="truncate-end" color={k.status === 'done' ? dim : k.status === 'active' ? hex(C.accent) : undefined}>
          {k.status === 'done' ? '✓' : k.status === 'active' ? '▸' : '○'} part {k.id} {k.title}
          <Text color={dim}>{k.steps_total ? `  ${k.steps_done ?? 0}/${k.steps_total}` : ''}</Text>
        </Text>
      )),
      ...(a.hypotheses ?? []).map(hy => (
        <Text key={`hyp-${hy.n}`} color={hex(hy.status === 'confirmed' ? C.ok : hy.status === 'ruled out' ? C.dim : C.warn)} wrap="truncate-end">
          {hy.status === 'confirmed' ? '✓' : hy.status === 'ruled out' ? '✗' : '?'} H{hy.n}. {hy.text}
        </Text>
      )),
      ...(a.oracle
        ? [
            <Text key="fm-pane-oracle" color={hex(a.oracle.ambiguities.length ? C.warn : C.dim)} wrap="truncate-end">
              oracle: {a.oracle.examples} example{a.oracle.examples === 1 ? '' : 's'}
              {a.oracle.ambiguities.length ? `, open: ${a.oracle.ambiguities[0]}` : ', no open questions'}
            </Text>,
          ]
        : []),
      ...((a.batch ?? []).length ? [<Text key="fm-pane-batch" color={dim} wrap="truncate-end">batch of {(a.batch ?? []).join(', ')}</Text>] : []),
      ...((a.inconclusive ?? 0) > 0
        ? [<Text key="fm-pane-inconclusive" color={hex(C.warn)} wrap="truncate-end">≈ {a.inconclusive} inconclusive run{a.inconclusive === 1 ? '' : 's'}: a sharper check is next</Text>]
        : []),
      ...((a.unverified ?? []).length
        ? [<Text key="fm-pane-unverified" color={dim} wrap="truncate-end">? {(a.unverified ?? []).length} unverified assumption{(a.unverified ?? []).length === 1 ? '' : 's'}: {(a.unverified ?? [])[0]}</Text>]
        : []),
    ]
    const railWant = a && stageIdx >= 0 ? 3 : 0 // the rail, its labels, a blank row
    const planRows = planQ ? 2 + (planQ.plan.interpretation ? 1 : 0) + (planQ.plan.approach ? 1 : 0) + planQ.plan.steps.length + planQ.plan.criteria.length : 0
    const stepBar = !planQ && a && a.steps.length ? 1 : 0 // T-1026: done and left, a segment a step
    const splitRow = !planQ && a?.splits?.length ? 1 : 0 // T-1078: speedrun splits, one compact row
    const taskWant = 2 + stepBar + splitRow + (planQ ? planRows : a ? railWant + a.steps.length + extras.length : (v.learned && !live ? 1 : 0) + (v.look && !live ? 1 : 0))

    // ── activity: one lane per kind of call, fixed in order so nothing jumps; fewer lanes on a short dock
    const nLanes = H >= 40 ? KINDS.length : H >= 30 ? 4 : 3
    const axis = H >= 30 ? 1 : 0
    const T = H >= 44 ? 6 : H >= 32 ? 5 : 4 // the Today charts' rows
    // ── agents: this session's subagents (running, and finished within the window), the git worktrees beside the
    // checkout (a builder's joins its agent's row) and a brainstorm: one Gantt row each (T-1023)
    const trees = v.worktrees ?? []
    const bsNow = v.brainstorm
    type Gantt = { key: string; glyph: string; color: number; start: number; end: number | null; marks: number[]; name: string;
      status: string; statusColor: number; live: boolean; stop?: string; dim?: boolean; child?: Gantt }
    const recentSubs = subs.filter(s => !s.done || clockNow - (s.lastAt ?? s.startedAt) < TIMELINE_MS) // the window, at every draw
    const treeNode = (w: NonNullable<typeof v.worktrees>[number]): Gantt => ({
      key: `tree-${w.branch || w.path}`, glyph: '⎇', color: w.ahead ? C.accent2 : C.dim, dim: true, live: false, marks: [],
      start: clockNow - (w.age_s ?? 0) * 1000, end: null, name: w.task ? `${w.task}  ${w.branch}` : w.branch || w.path,
      status: w.stopped ? `stopped · fm lane resume ${w.task}`
        : w.ahead === null ? (w.branch ? 'unknown' : 'detached') : w.ahead ? `${w.ahead} to merge`
        : w.open ? 'working' : 'merged', // T-1152: a fresh builder's branch has nothing ahead yet
      statusColor: w.ahead && !w.stopped ? C.accent2 : C.warn,
    })
    const gantt: Gantt[] = recentSubs.map(s => {
      const quiet = s.done ? 0 : clockNow - (s.lastAt ?? s.startedAt)
      const tree = trees.find(w => w.agent === s.id)
      return {
        key: s.id, glyph: '◆', color: quiet >= QUIET_MS ? C.warn : agentColor(s.id), start: s.startedAt, live: !s.done,
        end: s.done ? (s.lastAt ?? s.startedAt) : null, marks: s.calls ?? [], child: tree ? treeNode(tree) : undefined,
        name: `${s.type}${s.description ? `  ${s.description}` : ''}`,
        status: s.done ? `✓ ${s.tools} call${s.tools === 1 ? '' : 's'}` : quiet >= QUIET_MS ? `quiet ${elapsed(quiet)}` : elapsed(clockNow - s.startedAt),
        statusColor: quiet >= QUIET_MS ? C.warn : C.muted, stop: !s.done && quiet >= STOP_MS ? s.id : undefined,
      }
    })
    const seenIdeas = ideas && bsNow && ideas.name === bsNow.name ? ideas : null
    if (bsNow && (bsNow.running || (seenIdeas && clockNow - (seenIdeas.end ?? clockNow) < TIMELINE_MS)))
      gantt.push({
        key: 'ideas', glyph: '✦', color: C.skill, start: seenIdeas?.at ?? clockNow, end: bsNow.running ? null : seenIdeas?.end ?? clockNow,
        marks: seenIdeas?.answers ?? [], live: bsNow.running, statusColor: C.muted,
        name: `ideas${bsNow.ideas.length ? `: ${bsNow.ideas.at(-1)}` : ''}`,
        status: bsNow.running ? `${bsNow.answers}/${bsNow.expected ?? '?'}` : `✓ ${bsNow.count} ideas`,
      })
    for (const w of trees.filter(w => !recentSubs.some(s => s.id === w.agent))) gantt.push(treeNode(w))
    const rank = (g: Gantt) => (g.live ? 0 : g.glyph === '⎇' ? 2 : 1) // running, then finished, then lone worktrees
    gantt.sort((x, y) => rank(x) - rank(y) || x.start - y.start)
    // the graph's rows: the session at the root, then each child with the lines that join it (and its worktree under it)
    type Node = Gantt & { prefix: string; lineColor: number }
    const nodes: Node[] = !gantt.length ? [] : [
      { key: 'session', glyph: '●', color: C.accent, start: 0, end: null, marks: [], live: live, prefix: '', lineColor: C.track,
        name: `this session${a ? `  ${a.id}` : ''}`, status: '', statusColor: C.muted },
      ...gantt.flatMap((g, i) => {
        const last = i === gantt.length - 1
        const line = mix(C.track, g.color, 0.55)
        return [{ ...g, prefix: last ? '└─' : '├─', lineColor: line },
          ...(g.child ? [{ ...g.child, prefix: `${last ? '  ' : '│ '}└─`, lineColor: line }] : [])]
      }),
    ]
    // ── T-3701: the Board's instruments, each from its slice of the view: LANES (a node cluster per lane), MERGE (the
    // waiting lanes flowing into the build), CPU (the Sampo sessions stacked against the goal), CHECKS (a cell a gate)
    const inLanes = new Set([...ln.building, ...ln.merging, ...ln.closing].map(l => l.id))
    const laneNodes = [
      ...ln.building.map(l => ({ id: l.id, color: C.accent2, done: l.steps_done, total: l.steps_total, word: 'building' })),
      ...ln.merging.map(l => ({ id: l.id, color: C.accent, done: l.steps_done, total: l.steps_total, word: 'waiting' })),
      ...trees.filter(w => w.stopped && w.task && !inLanes.has(w.task)).map(w => ({ id: w.task!, color: C.agent, done: 0, total: 1, word: 'parked' })),
      ...ln.closing.map(l => ({ id: l.id, color: C.ok, done: l.steps_done, total: l.steps_total, word: 'merged' })),
    ]
    const lanesWant = laneNodes.length ? (H >= 40 ? 5 : 4) : 0 // the ids, the clusters (2 rows up), their states
    // T-3701: no call in the window and none running: no tools panel, not six empty lanes
    const toolsIdle = !nowCalls.length && !bgShells.length && !ticks.some(t => clockNow - t.at <= TIMELINE_MS)
    const mergeWant = ln.merging.length || ln.closing.length ? Math.max(2, Math.min(4, Math.max(ln.merging.length, ln.closing.length))) : 0
    const cpu = v.cpu && v.cpu.sessions.length ? v.cpu : null
    const cpuWant = cpu ? (H >= 40 ? 4 : 3) : 0
    const checkList = checks?.results ?? []
    const checkW = Math.max(2, Math.min(W - 22, checkList.length * 3 - 1))
    const checksWant = checkList.length ? Math.min(3, Math.max(checkCells(checkList, checkW).rows, 1 + Math.min(2, checkList.filter(r => r.exit).length))) : 0 // the cells, or the count and what fails
    // T-3113: the crest takes the pane's top rows; every section is placed in what is left under it
    const mood = live ? 'work' : clockNow < happyUntil ? 'happy' : 'idle'
    const crestTop = crestTree(ui, e, W, H, mood, live ? (f ?? 0) : idleBeat, subs)
    const L = layout(H - crestTop.rows, [ // in the order rows are shared out: the task first, the history last
      { key: 'task', want: taskWant, cap: Math.max(4, Math.round(H * 0.4)), min: Math.min(taskWant, H >= 50 ? 9 : 4), spare: true }, // T-3701: the rail and steps before the instruments
      { key: 'activity', want: toolsIdle ? 0 : 1, fixed: toolsIdle ? 0 : nLanes + axis, grow: toolsIdle ? 0 : KINDS.length - nLanes }, // spare rows: every lane
      { key: 'lanes', want: lanesWant, cap: lanesWant, min: Math.min(lanesWant, 4), whole: true },
      { key: 'merge', want: mergeWant, cap: mergeWant },
      { key: 'agents', want: nodes.length, cap: Math.max(3, Math.round(H * 0.18)), min: Math.min(nodes.length, H >= 40 ? 5 : 3) }, // the root and its children, not just the root
      { key: 'queue', want: queueRows.length, cap: Math.max(4, Math.round(H * 0.2)), min: plans.length + 3 }, // the header, every plan's approve
      { key: 'cpu', want: cpuWant, cap: cpuWant, min: Math.min(cpuWant, 3), whole: true },
      { key: 'checks', want: checksWant, cap: checksWant, min: 1 },
      // T-3701: Today after the instruments (its gates tank is CHECKS in small); spare rows still make its charts taller
      { key: 'today', want: T, cap: T, min: T, grow: Math.max(0, 8 - T), whole: true },
      { key: 'sessions', want: sessionRows.length, cap: 5, min: Math.min(sessionRows.length, 3) },
      { key: 'history', want: historyRows.length + (shown.length >= 3 ? 1 : 0), stretch: true }, // its lines and their area
    ])
    const R = L.rows

    // ── head: who and how, the mascot on the right
    const modes: [string, number][] = m
      ? [
          [m.autonomy === 'full' ? 'full auto' : 'standard', m.autonomy === 'full' ? C.accent2 : C.dim],
          ...(m.drive ? [] : ([['drive off', C.warn]] as [string, number][])),
          ...(m.sensitive ? ([['sensitive', C.warn]] as [string, number][]) : []),
          ...(m.trust ? ([['trust on', C.warn]] as [string, number][]) : []),
          ...(m.risk_off ? ([['risk off', C.warn]] as [string, number][]) : []),
          ...(v.loop?.on ? ([[`∞ round ${v.loop.round}${v.loop.dry ? ` · ${v.loop.dry} dry` : ''}`, C.agent]] as [string, number][]) : []), // T-1057
        ]
      : []
    // T-1026 ('doesn't show how many things have been complete and how many are left'): the work as one bar
    const work = [
      { n: v.today_done ?? 0, word: 'done today', color: C.steel },
      { n: a ? 1 : 0, word: 'now', color: C.accent },
      { n: ln.building.length, word: 'building', color: C.agent },
      { n: ln.merging.length + ln.closing.length, word: 'to land', color: C.accent2 },
      { n: queueN, word: 'queued', color: C.accent2 },
      { n: inboxN, word: 'inbox', color: C.muted },
      { n: v.blocked_total ?? 0, word: 'blocked', color: C.warn },
    ]
    const leftParts = work.slice(1).filter(x => x.n)
    const doneWord = `${work[0]!.n} done today`
    const leftWord = `${leftParts.reduce((n, x) => n + x.n, 0)} left`
    const mascotW = cfg.mascot === 'off' || cfg.mascot === 'sampo' ? 0 : 12 + Math.min(4, subs.filter(x => !x.done).length) * 8 // and its minis
    // done on the bar's filled side, left on its track side; the breakdown after them gives way first
    const workW = Math.max(0, Math.min(28, W - mascotW - doneWord.length - leftWord.length - 4 - 22))
    const head = (
      <Box key="fm-pane-head" flexDirection="row" height={3} overflow="hidden" flexShrink={0}>
        <Box flexDirection="column" flexGrow={1} flexShrink={1}>
          <Text key="head-who" wrap="truncate-end">
            <Text weight="bold" color={hex(C.accent)}>
              {paneMark(moving, f ?? 0)} Sampo
            </Text>
            <Text color={muted}>{`  ${v.project}`}</Text>
            {modes.map(([w, c], i) => (
              <Text key={`mode-${i}`} color={hex(c)}>{`   ${w}`}</Text>
            ))}
          </Text>
          <Box key="head-work" flexDirection="row" gap={1} height={1} overflow="hidden">
            <Box flexShrink={0}>
              <Text weight="bold" color={hex(C.steel)}>{doneWord}</Text>
            </Box>
            {term && workW >= 6 && (
              <Box flexShrink={0}>
                <Raster key="work-bar" columns={workW} rows={1} cells={stackCells(work, workW)} />
              </Box>
            )}
            <Box flexShrink={0}>
              <Text weight="bold" color={hex(0xd0d5dd)}>{leftWord}</Text>
            </Box>
            <Text key="work-parts" wrap="truncate-end">
              {leftParts.map((x, i) => (
                <Text key={`work-${x.word}`} color={hex(x.color)}>{`${i ? ' · ' : ' '}${x.n} ${x.word}`}</Text>
              ))}
            </Text>
          </Box>
          {attention}
        </Box>
        {mascotTree(ui, e, mood, live ? (f ?? 0) : idleBeat, subs)}
      </Box>
    )

    // ── the task: its type and id with the time on it, the title, the stage rail, the steps, what proves it done
    const fit = <X,>(xs: X[], k: number, at: number) => {
      if (xs.length <= k) return xs
      const s = Math.max(0, Math.min(xs.length - k, k > 1 ? at - 1 : at)) // one done step above the current, room permitting
      return xs.slice(s, s + k)
    }
    const idLine = (left: RenderElement, right: string, rightColor: number) => (
      <Box key="task-id" flexDirection="row" justifyContent="space-between" gap={2} height={1} overflow="hidden">
        {left}
        <Box flexShrink={0}>
          <Text color={hex(rightColor)}>{right}</Text>
        </Box>
      </Box>
    )
    const taskRows: RenderElement[] = []
    const weight: number[] = [] // the steps, criteria or notes each row stands for (the head and the rail: none)
    const push = (w: number, ...xs: RenderElement[]) => xs.forEach(x => void (taskRows.push(x), weight.push(w)))
    let hidden = 0 // steps outside the window
    if (planQ) {
      const p = planQ.plan
      push(
        0,
        idLine(
          <Box key="plan-chip" flexDirection="row" gap={1} flexShrink={0}>
            <Text weight="bold" color={hex(C.warn)}>Plan to approve</Text>
            <Text color={hex(C.accent)}>{planQ.id}</Text>
          </Box>,
          `${p.steps.length} steps`,
          C.dim,
        ),
        <Text key="plan-title" weight="bold" wrap="truncate-end">{planQ.title}</Text>,
      )
      push(
        1,
        ...(p.interpretation ? [<Text key="plan-means" wrap="truncate-end"><Text color={dim}>means     </Text>{p.interpretation}</Text>] : []),
        ...(p.approach ? [<Text key="plan-approach" wrap="truncate-end"><Text color={dim}>approach  </Text>{p.approach}</Text>] : []),
        ...p.steps.map(s => <Text key={`plan-step-${s.n}`} wrap="truncate-end"><Text color={dim}>{`○ ${String(s.n).padStart(2)}  `}</Text>{s.text}</Text>),
        ...p.criteria.map(c => <Text key={`plan-ac-${c.n}`} color={dim} wrap="truncate-end">✓ when {c.text}</Text>),
      )
    } else if (a) {
      const over = usual !== undefined && onMin !== null && onMin > usual
      push(
        0,
        idLine(
          <Box key="task-chip" flexDirection="row" gap={1} flexShrink={0}>
            <Text weight="bold" color={typeColor(a.type)}>{a.type}</Text>
            <Text weight="bold" color={hex(C.accent)}>{a.id}</Text>
            <Text color={muted}>{sizeWord(a.tier)}</Text>
          </Box>,
          [a.criteria.length ? `${a.criteria.filter(c => c.checked).length}/${a.criteria.length} proved` : '',
            onMin === null ? '' : `${elapsed(onMin * 60000)}${usual !== undefined ? ` of ${about(usual)}` : ''}`].filter(Boolean).join('   '),
          over ? C.warn : C.muted,
        ),
        <Text key="task-title" weight="bold" wrap="truncate-end">{a.title}</Text>,
      )
      if (stepBar) {
        const left = a.steps.filter(s => !s.done).length
        const label = `${a.steps.length - left} of ${a.steps.length} steps done · ${left} left`
        const sw = Math.max(6, W - label.length - 2)
        if (blit && ft !== null) blit.set('task-steps', { columns: sw, rows: 1, draw: t => stepCells(a.steps, sw, t) })
        push(0,
          <Box key="task-progress" flexDirection="row" gap={2} height={1} overflow="hidden">
            {term && (
              <Box flexShrink={0}>
                <Raster key="task-steps" columns={sw} rows={1} cells={stepCells(a.steps, sw, ft)} />
              </Box>
            )}
            <Text color={hex(left ? C.muted : C.ok)} wrap="truncate-end">{label}</Text>
          </Box>,
        )
      }
      if (splitRow)
        push(0,
          <Box key="task-splits" flexDirection="row" gap={1} height={1} overflow="hidden">
            {a.splits!.map(s => (
              <Text key={`split-${s.stage}`} wrap="truncate-end"
                color={hex(s.sign === 'over' ? C.err : s.sign === 'under' ? C.ok : C.dim)}>
                {`${s.stage} ${elapsed(s.secs * 1000)}${s.delta == null ? '' : ` ${s.delta < 0 ? '−' : '+'}${elapsed(Math.abs(s.delta) * 1000)}`}`}
              </Text>
            ))}
          </Box>,
        )
      const rows = R.task! - 2 - stepBar - splitRow
      const railRows = railWant && rows >= Math.min(6, taskWant - 2 - stepBar - splitRow) ? 3 : 0
      const room = Math.max(1, rows - railRows)
      // every step and note when they fit; else a window round the current step and a row to count the rest
      const nSteps = a.steps.length + extras.length <= room ? a.steps.length : Math.max(1, Math.min(a.steps.length, room - extras.length - 1))
      const at = Math.max(0, a.steps.findIndex(s => s.current))
      if (railRows)
        push(0, <Box key="fm-pane-rail" flexDirection="column">{rail(ui, e, a.stages, stageIdx, W, ft, blit)}</Box>, <Text key="rail-gap"> </Text>)
      hidden = a.steps.length - nSteps
      push(
        1,
        ...fit(a.steps, nSteps, at).map(s => {
          const lit = flashed.get(`${a.id}:${s.n}`)
          const k = lit === undefined ? 1 : (clockNow - lit) / FLASH_MS
          return (
            <Text key={`step-${s.n}`} wrap="truncate-end"
              color={s.current ? hex(C.hot) : s.done ? hex(k < 1 ? C.steel : C.dim) : undefined}
              backgroundColor={k < 1 ? hex(mix(mix(C.ok, BG, 0.62), BG, k)) : undefined}>
              <Text color={hex(s.done ? C.steel : s.current ? C.hot : C.dim)}>{s.done ? '✓' : s.current ? (moving ? spin(f ?? 0) : '▸') : '○'}</Text>
              {` ${String(s.n).padStart(2)}  ${s.text}`}
            </Text>
          )
        }),
        ...extras,
      )
      // rows to spare (no History yet to take them): what proves it done, all of it or none
      const spare = R.task! - taskRows.length - (hidden ? 1 : 0)
      if (!hidden && a.criteria.length && spare >= a.criteria.length + 2)
        push(
          0,
          <Text key="ac-gap"> </Text>,
          <Text key="ac-head" color={soft}>done when</Text>,
          ...a.criteria.map(c => (
            <Text key={`ac-${c.n}`} color={c.checked ? hex(C.ok) : undefined} wrap="truncate-end">
              <Text color={hex(c.checked ? C.ok : C.dim)}>{c.checked ? '✓ ' : '○ '}</Text>
              {c.text}
            </Text>
          )),
        )
    } else {
      push(
        0,
        idLine(
          <Text key="idle-word" color={dim}>{live ? 'Working, no task in focus' : 'Nothing active'}</Text>,
          `${v.today_done ?? 0} closed today`,
          C.ok,
        ),
        <Text key="idle-next" wrap="truncate-end">
          {v.next ? <Text color={dim}>Next  </Text> : null}
          {v.next ? humanNext(v.next) : <Text color={dim}>The queue and the inbox are empty.</Text>}
        </Text>,
        ...(v.learned && !live ? [<Text key="idle-learned" color={dim} wrap="truncate-end">{v.learned}</Text>] : []), // T-1045
        ...(v.look && !live ? [<Text key="idle-look" color={dim} wrap="truncate-end">{v.look}</Text>] : []), // T-1672
      )
    }
    if (taskRows.length + (hidden ? 1 : 0) > R.task!) { // never cut silently (T-0146): the last row counts what's gone
      const keep = Math.max(0, R.task! - 1)
      hidden += weight.slice(keep).reduce((x, y) => x + y, 0)
      taskRows.splice(keep)
    }
    if (hidden) taskRows.push(<Text key="task-more" color={dim}>{`… ${hidden} more`}</Text>)

    // ── activity: each lane plots its calls over the window; its end names what runs there now, else its count
    const lastTick = ticks.at(-1)
    const spanMs = Math.ceil(Math.min(TIMELINE_MS, Math.max(60000, ticks.length ? clockNow - ticks[0]!.at : TIMELINE_MS)) / 60000) * 60000
    const recentTicks = ticks.filter(t => clockNow - t.at <= spanMs)
    const LBL = 6
    const RW = Math.max(12, Math.min(34, Math.floor(W * 0.4))) // the count, then what runs there now or ran last
    const laneW = Math.max(8, W - LBL - RW - 1)
    type Now = { key: string; text: string; time: string; color: number; stop?: string }
    const nowIn: Record<string, Now[]> = {}
    const put = (k: string, x: Now) => void (nowIn[k] ??= []).push(x)
    nowCalls.filter(([, x]) => x.icon !== '⚠').forEach(([id, x]) =>
      put(laneOf(x.icon), { key: id, text: x.target || x.verb, time: elapsed(clockNow - x.at), color: x.color }))
    bgShells.forEach(x => put('run', { key: x.id, text: `◷ ${x.command}`, time: elapsed(clockNow - x.startedAt), color: C.accent2 }))
    const nLive = Object.values(nowIn).reduce((n, xs) => n + xs.length, 0)
    const counts = Object.fromEntries(KINDS.map(k => [k, recentTicks.filter(t => t.lane === k).length])) as Record<string, number>
    const lanes = Math.max(1, Math.min(KINDS.length, R.activity! - axis)) // more when the dock has rows to spare
    const busiest = [...KINDS].sort((x, y) => counts[y]! - counts[x]!).slice(0, lanes) // by the window only: nothing swaps as calls start
    const laneRows: RenderElement[] = KINDS.filter(k => busiest.includes(k)).map(k => {
      const n = counts[k]!
      const color = C[KIND_COLOR[k]!]
      const slots = bucketize(recentTicks.filter(t => t.lane === k).map(t => t.at), clockNow, spanMs, laneW * 2)
      if (blit && ft !== null && n) blit.set(`lane-r-${k}`, { columns: laneW, rows: 1, draw: t => laneCells([{ color, slots }], laneW, t) })
      const here = nowIn[k] ?? []
      const x = here[0]
      return (
        <Box key={`lane-${k}`} flexDirection="row" height={1} overflow="hidden">
          <Box flexShrink={0} width={LBL}>
            <Text color={hex(n || x ? color : C.dim)}>{k}</Text>
          </Box>
          <Box flexShrink={0}>
            {raster(`lane-r-${k}`, laneW, laneCells([{ color, slots }], laneW, n ? ft : null), n ? sparkText(slots.filter((_, i) => i % 2 === 0), laneW) : '⣀'.repeat(laneW), color)}
          </Box>
          <Box flexShrink={0} width={RW + 1} flexDirection="row" gap={1}>
            <Box key="lane-n" flexShrink={0} width={4} flexDirection="row" justifyContent="flex-end">
              {x ? <Text color={hex(x.color)}>{moving ? spin((f ?? 0) + here.length) : '▸'}</Text> : <Text color={dim}>{n ? String(n) : ''}</Text>}
            </Box>
            {x
              ? [
                  <Text key="now-text" wrap="truncate-end" color={hex(mix(x.color, 0xc8ccd4, 0.5))}>{x.stop ? `${x.time}  ${x.text}` : x.text}</Text>,
                  ...(x.stop // a narrow dock keeps the button: the time goes into the text, which gives way first
                    ? [<Button key={`stop-${x.stop}`} label="■ stop" plain onPress={() => void stopAgent($, x.stop!)} />]
                    : [<Box key="now-time" flexShrink={0}><Text color={dim}>{`${x.time}${here.length > 1 ? ` +${here.length - 1}` : ''}`}</Text></Box>]),
                ]
              : <Text key="lane-last" wrap="truncate-end" color={muted}>{n ? (recentTicks.filter(t => t.lane === k).at(-1)?.what ?? '') : ''}</Text>}
          </Box>
        </Box>
      )
    })
    const axisRow = (
      <Box key="lane-axis" flexDirection="row" height={1} overflow="hidden">
        <Box flexShrink={0} width={LBL} />
        <Text color={soft}>{`${spanMs / 60000}m ago`.padEnd(Math.max(1, laneW - 3))}now</Text>
      </Box>
    )
    const gLive = gantt.filter(g => g.live).length
    const gStarts = gantt.filter(g => g.glyph !== '⎇').map(g => g.start) // a worktree's age doesn't stretch the axis
    const earliest = gStarts.length ? Math.min(...gStarts) : clockNow - TIMELINE_MS
    const gSpan = Math.ceil(Math.min(60, Math.max(2, (clockNow - earliest) / 60000))) * 60000 // its own axis, 2 to 60 min
    const gw = Math.max(8, Math.floor(W * 0.38)) // the bars, in one column; the tree and names on the left
    const STW = 12 // the status column
    const agentRows: RenderElement[] = nodes.map(g => {
      const isRoot = g.key === 'session'
      const node = isRoot // the root's bar: every call this session made in the window
        ? { ...g, start: clockNow - gSpan, marks: recentTicks.map(t => t.at), status: `${recentTicks.length} calls` }
        : g
      return (
        <Box key={`agent-row-${g.key}`} flexDirection="row" gap={1} height={1} overflow="hidden">
          <Box flexGrow={1} flexShrink={1} flexDirection="row" overflow="hidden">
            {g.prefix ? (
              <Box flexShrink={0}>
                <Text color={hex(g.lineColor)}>{g.prefix}</Text>
              </Box>
            ) : null}
            <Box flexShrink={0}>
              <Text color={hex(g.color)}>{`${g.live && moving && !isRoot ? spin((f ?? 0) + g.start) : g.glyph} `}</Text>
            </Box>
            <Text wrap="truncate-end" weight={isRoot ? 'bold' : 'regular'} color={hex(g.stop ? C.warn : isRoot ? 0xd0d5dd : mix(g.color, 0xd0d5dd, g.dim ? 0.45 : 0.6))}>
              {g.stop ? `${g.status}  ${g.name}` : g.name}
            </Text>
          </Box>
          {term && (
            <Box flexShrink={0}>
              <Raster key={`gantt-${g.key}`} columns={gw} rows={1} cells={ganttCells(node, clockNow - gSpan, gSpan, gw, clockNow, g.live ? ft : null)} />
            </Box>
          )}
          {g.stop // a narrow dock keeps the button: the time went into the name, which gives way first
            ? ( // in the status column's width, so every bar keeps one time axis (T-1026 review)
              <Box key="gantt-stop" flexShrink={0} width={STW} flexDirection="row" justifyContent="flex-end">
                <Button key={`stop-${g.stop}`} label="■ stop" plain onPress={() => void stopAgent($, g.stop!)} />
              </Box>
            )
            : (
              <Box key="gantt-status" flexShrink={0} width={STW} flexDirection="row" justifyContent="flex-end">
                <Text color={hex(g.statusColor)} wrap="truncate-end">{node.status}</Text>
              </Box>
            )}
        </Box>
      )
    })
    const activityRight = nLive
      ? `${nLive} running`
      : lastTick ? `${recentTicks.length} calls, last ${since(clockNow - lastTick.at)}` : 'no calls yet'

    // ── today: closes per day as tall columns, beside four tank gauges that read as full or not at a glance
    const bw = days.length ? Math.max(1, Math.min(3, Math.floor((Math.floor(W / 2) + 1) / days.length) - 1)) : 0
    const daysW = term ? days.length * (bw + 1) - 1 : days.length // off the terminal a sparkline, a column a day
    const showDays = days.length > 0 && W - daysW - 4 >= 4 * 6 + 6 // the four tanks, 6 wide, first
    const leftW = showDays ? daysW : 0
    const GAP = showDays ? 4 : 0
    const rightW = Math.max(8, W - leftW - GAP)
    const b = v.budget
    const p95 = v.health?.hook_p95_ms
    const levels: { label: string; value: string; frac: number; color: number }[] = [
      { label: 'context', value: cx ? `${Math.round(cx.percent)}%` : '–', frac: cx ? cx.percent / 100 : 0, color: cx ? pctColor(cx.percent) : C.dim },
      ...(b
        ? [{ label: 'spend', value: `$${b.today_usd < 10 ? b.today_usd.toFixed(2) : Math.round(b.today_usd)}`, frac: b.today_usd / Math.max(0.01, b.caps.day),
            color: b.today_usd >= 0.9 * b.caps.day || b.halved ? C.warn : C.accent2 }]
        : [{ label: 'spend', value: '–', frac: 0, color: C.dim }]),
      { label: 'hooks', value: typeof p95 === 'number' ? `${p95}ms` : '–', frac: typeof p95 === 'number' ? Math.min(1, p95 / 300) : 0,
        color: v.health?.hook_errors ? C.err : typeof p95 === 'number' ? heat(Math.min(1, p95 / 300)) : C.dim },
      { label: 'gates', value: checks?.results.length ? `${passed}/${checks.results.length}` : '–', frac: checks?.results.length ? passed / checks.results.length : 0,
        color: failing ? C.err : C.ok },
    ]
    const tw = Math.max(3, Math.floor((rightW - 2 * (levels.length - 1)) / levels.length))
    const tk = Math.min(tw, 6) // a tank's width, centred in its column
    const tanksW = levels.length * tk + (tw - tk + 2) * (levels.length - 1)
    const centre = (s: string, w: number) => {
      const t = s.slice(0, w)
      const l = Math.floor((w - t.length) / 2)
      return ' '.repeat(l) + t + ' '.repeat(w - t.length - l)
    }
    const TR = Math.max(1, R.today! - 2) // the tanks' rows, between the values and the labels
    const todayBody: RenderElement[] = R.today! > 0 ? [
      <Box key="today-row" flexDirection="row" gap={GAP} height={R.today!} overflow="hidden">
        {showDays && (
          <Box key="today-days" flexDirection="column" flexShrink={0}>
            {bars(ui, e, 'done-days', days, Math.max(1, R.today! - 1), C.accent2, C.ok, bw)}
            <Text color={muted}>{`14 days ago${' '.repeat(Math.max(1, leftW - 11 - `${v.today_done ?? 0} today`.length))}`}<Text color={hex(C.ok)}>{`${v.today_done ?? 0} today`}</Text></Text>
          </Box>
        )}
        <Box key="today-tanks" flexDirection="column" flexShrink={0}>
          <Text key="tank-values">
            {levels.map((l, i) => (
              <Text key={`tv-${l.label}`} weight="bold" color={hex(l.value === '–' ? C.dim : l.color)}>{`${i ? '  ' : ''}${centre(l.value, tw)}`}</Text>
            ))}
          </Text>
          {term ? (
            <Box key="fm-tanks" flexShrink={0} paddingLeft={Math.floor((tw - tk) / 2)}>
              <Raster key="fm-tank-cells" columns={tanksW} rows={TR} cells={tankCells(levels, TR, tk, tw - tk + 2)} />
            </Box>
          ) : (
            <Text key="fm-tanks" color={dim}>{levels.map(l => `${l.label} ${Math.round(l.frac * 100)}%`).join('  ')}</Text>
          )}
          <Text key="tank-labels" color={muted}>{levels.map((l, i) => `${i ? '  ' : ''}${centre(l.label, tw)}`).join('')}</Text>
        </Box>
      </Box>,
    ] : []

    // ── T-3701: the Board's instruments. Each takes the rows the layout gave it; off the terminal, its text twin.
    const pad = (n: number) => ' '.repeat(Math.max(0, n))
    const lanesBody = (rows: number): RenderElement[] => {
      if (!laneNodes.length || rows <= 0) return []
      const k = Math.max(1, Math.min(laneNodes.length, Math.floor(W / 10)))
      const shownL = laneNodes.slice(0, k)
      const sw = Math.floor(W / k)
      const more = laneNodes.length - k
      const ids = (
        <Box key="lanes-ids" flexDirection="row" height={1} overflow="hidden">
          {shownL.map((l, i) => (
            <Box key={`id-${l.id}`} flexShrink={0} width={sw} flexDirection="row">
              <Text>{pad(Math.floor((sw - l.id.length - 2) / 2))}</Text>
              {pill(ui, `lane-pill-${i}`, l.id, l.color)}
            </Box>
          ))}
        </Box>
      )
      const states = (
        <Text key="lanes-states" color={dim} wrap="truncate-end">
          {shownL.map((l, i) => centre(i === k - 1 && more ? `+${more} more` : `${l.word} ${l.done}/${l.total}`, sw)).join('')}
        </Text>
      )
      const gRows = rows - 2
      if (gRows < 1) return rows === 1 ? [ids] : [ids, states]
      const graph = term
        ? <Raster key="lanes-graph" columns={k * sw} rows={gRows} cells={laneClusters(shownL, k * sw, gRows)} />
        : <Box key="lanes-graph"><Text color={muted}>{shownL.map(l => centre(`◉ ${'●'.repeat(Math.min(6, l.done))}${'○'.repeat(Math.min(6, Math.max(0, l.total - l.done)))}`, sw)).join('')}</Text></Box>
      return [ids, graph, states]
    }
    const mergeBody = (rows: number): RenderElement[] => {
      if (rows <= 0) return []
      const src = ln.merging.slice(0, rows)
      const extra = ln.merging.length - src.length
      const landed = ln.closing
      const RW = landed.length && W >= 50 ? 9 : 0
      const BW = 10
      const LW = Math.max(12, Math.min(28, Math.floor(W * 0.38)))
      const FW = Math.max(4, W - LW - BW - RW - 2)
      const target = Math.floor((rows - 1) / 2)
      const at = Array.from({ length: rows }, (_, i) => i)
      const box = mix(C.bg, C.ok, 0.22)
      return [
        <Box key="merge-row" flexDirection="row" height={rows} overflow="hidden">
          <Box key="merge-src" flexDirection="column" flexShrink={0} width={LW}>
            {at.map(i => {
              const l = src[i]
              if (!l) return <Text key={`src-${i}`} color={dim}>{i === src.length && extra ? `+${extra} more` : ' '}</Text>
              return (
                <Box key={`src-${i}`} flexDirection="row" gap={1} height={1} overflow="hidden">
                  <Box flexShrink={0}><Text weight="bold" color={hex(C.accent)}>{l.id}</Text></Box>
                  <Box flexShrink={1} flexGrow={1}><Text wrap="truncate-end" color={muted}>{l.title}</Text></Box>
                  <Box flexShrink={0}>{meter(ui, e, `src-bar-${i}`, 4, l.steps_total ? l.steps_done / l.steps_total : 1, C.accent, null)}</Box>
                </Box>
              )
            })}
          </Box>
          <Box key="merge-gap" flexShrink={0} width={1} />
          {term
            ? <Raster key="merge-flow" columns={FW} rows={rows} cells={flowCells(src.map(() => C.accent), target, FW, rows)} />
            : (
              <Box key="merge-flow" flexDirection="column" flexShrink={0} width={FW}>
                {at.map(i => <Text key={`fl-${i}`} color={hex(C.accent)}>{src[i] ? `${'─'.repeat(Math.max(1, FW - 1))}${i === target ? '▶' : '╯'}` : i === target ? `${pad(FW - 1)}▶` : ' '}</Text>)}
              </Box>
            )}
          <Box key="merge-box" flexDirection="column" flexShrink={0} width={BW}>
            {at.map(i => i === target
              ? <Text key={`box-${i}`} weight="bold" color={hex(C.ink)} backgroundColor={hex(box)}>{' ▣ build '}</Text>
              : i === target + 1 && landed.length ? <Text key={`box-${i}`} color={dim}>{` ${landed.length} landed`.slice(0, BW)}</Text>
              : <Text key={`box-${i}`}> </Text>)}
          </Box>
          {RW ? (
            <Box key="merge-landed" flexDirection="column" flexShrink={0} width={RW}>
              {at.map(i => <Text key={`ld-${i}`} color={hex(C.ok)}>{landed[i] ? (i === rows - 1 && landed.length > rows ? `+${landed.length - i}` : `✓ ${landed[i]!.id}`) : ' '}</Text>)}
            </Box>
          ) : null}
        </Box>,
      ]
    }
    const CPU_COLORS = [C.accent2, C.edit, C.accent, C.agent, C.ok, C.warn]
    const cpuSessions = (cpu?.sessions ?? []).slice(0, CPU_COLORS.length)
    const cpuTotals = Array.from({ length: Math.max(0, ...cpuSessions.map(s => s.pct.length)) }, (_, j) => cpuSessions.reduce((t, s) => t + (s.pct[j] ?? 0), 0))
    const cpuNow = cpu ? cpu.now ?? cpuTotals.at(-1) ?? 0 : 0
    const cpuBody = (rows: number): RenderElement[] => {
      if (!cpu || rows <= 0) return []
      const over = cpuNow > cpu.goal
      const top = Math.max(cpu.goal * 1.5, Math.max(0, ...cpuTotals) * 1.1, 1)
      const LW = W >= 60 ? 19 : W >= 50 ? 16 : 11, AX = 4
      const HW = Math.max(6, W - LW - AX - 2)
      const goalRow = rows - 1 - Math.min(rows - 1, Math.floor(Math.round((cpu.goal / top) * rows * 8) / 8)) // from the top
      const legend = cpuSessions.map((s, i) => (
        <Text key={`cpu-l-${i}`} wrap="truncate-end">
          <Text color={hex(CPU_COLORS[i]!)}>■ </Text>
          <Text color={muted}>{(s.name.length > LW - 8 ? `${s.name.slice(0, Math.ceil((LW - 9) / 2))}…${s.name.slice(-Math.floor((LW - 9) / 2))}` : s.name).padEnd(LW - 8)}</Text>
          <Text color={hex(C.ink)}>{(s.pct.at(-1) ?? 0).toFixed(1).padStart(4)}</Text>
        </Text>
      ))
      const left = [
        <Text key="cpu-now" wrap="truncate-end"><Text weight="bold" color={hex(over ? C.err : C.ok)}>{`${cpuNow.toFixed(1)}%`}</Text><Text color={dim}> now</Text></Text>,
        <Text key="cpu-goal" color={over ? hex(C.err) : dim} wrap="truncate-end">{over ? `over the ${cpu.goal}% goal` : `goal ${cpu.goal}%`}</Text>,
        ...legend,
      ].slice(0, rows)
      return [
        <Box key="cpu-row" flexDirection="row" gap={1} height={rows} overflow="hidden">
          <Box key="cpu-left" flexDirection="column" flexShrink={0} width={LW}>{left}</Box>
          {term
            ? <Raster key="cpu-hist" columns={HW} rows={rows} cells={histCells(cpuSessions.map((s, i) => ({ color: CPU_COLORS[i]!, values: s.pct })), HW, rows, top, cpu.goal)} />
            : <Box key="cpu-hist" flexShrink={0} width={HW}><Text color={hex(C.accent2)}>{sparkText(cpuTotals, HW)}</Text></Box>}
          <Box key="cpu-axis" flexDirection="column" flexShrink={0} width={AX}>
            {Array.from({ length: rows }, (_, i) => <Text key={`ax-${i}`} color={dim}>{i === goalRow ? `${cpu.goal}%`.slice(0, AX) : ' '}</Text>)}
          </Box>
        </Box>,
      ]
    }
    const failed = checkList.filter(r => r.exit)
    const checksBody = (rows: number): RenderElement[] => {
      if (!checkList.length || rows <= 0) return []
      const g = checkCells(checkList, checkW)
      const legend = [
        <Text key="ck-count" wrap="truncate-end">
          <Text color={hex(C.ok)}>{`✓ ${passed} pass`}</Text>
          {failed.length ? <Text color={hex(C.err)}>{`  ✗ ${failed.length} fail`}</Text> : null}
        </Text>,
        ...failed.map((r, i) => <Text key={`ck-f-${i}`} color={hex(C.err)} wrap="truncate-end">{`✗ ${r.cmd}${r.note ? ` · ${r.note}` : ''}`}</Text>),
      ].slice(0, Math.max(1, rows))
      return [
        <Box key="checks-row" flexDirection="row" gap={2} height={rows} overflow="hidden">
          {term
            ? <Raster key="checks-cells" columns={g.columns} rows={Math.min(g.rows, rows)} cells={g.cells} />
            : <Box key="checks-cells" flexShrink={0}><Text>{checkList.map((r, i) => <Text key={`c-${i}`} color={hex(r.exit ? C.err : C.ok)}>■ </Text>)}</Text></Box>}
          <Box key="checks-legend" flexDirection="column" flexShrink={1}>{legend}</Box>
        </Box>,
      ]
    }
    // History: an area of the events over their span (failures on top, in red), above the lines
    const histSpark = (() => {
      if (shown.length < 3) return []
      let last = -1, base = 0
      const mins = shown.map(ev => {
        const m = /^(\d\d):(\d\d)$/.exec(ev.time)
        const t = m ? Number(m[1]) * 60 + Number(m[2]) : last
        if (t + base < last - 720) base += 1440 // past midnight
        last = t + base
        return last
      })
      if (mins[0]! < 0) return []
      const nb = Math.max(4, Math.min(24, Math.floor((W + 1) / 3)))
      const span = Math.max(1, mins.at(-1)! - mins[0]!)
      const bucket = (i: number) => Math.min(nb - 1, Math.floor(((mins[i]! - mins[0]!) / span) * nb))
      const ok = Array(nb).fill(0) as number[], bad = Array(nb).fill(0) as number[]
      shown.forEach((ev, i) => void ('✗✖'.includes(ev.mark) ? bad : ok)[bucket(i)]!++)
      const top = Math.max(1, ...ok.map((x, i) => x + bad[i]!))
      return [term
        ? <Raster key="history-spark" columns={W} rows={1} cells={histCells([{ color: C.accent2, values: ok }, { color: C.err, values: bad }], W, 1, top, null)} />
        : <Box key="history-spark" height={1}><Text color={hex(C.accent2)}>{sparkText(ok.map((x, i) => x + bad[i]!), W)}</Text></Box>]
    })()

    // ── drawing (T-3701): each section a Board panel: its head (┌ the number on its accent, NAME · what, the label ┐),
    // exactly its rows (what doesn't fit is counted, never cut silently), and on a tall dock, where a blank row parted
    // sections, its footer (└ a caption, the stat ┘). Numbered in the order drawn, the chips' colours in the Board's turn.
    const body = (key: string, rows: number, xs: RenderElement[], tall = 0, log = false) => {
      const over = xs.length + tall > rows
      const shown = log || !over // a log just loses its oldest lines; never more rows than fit, or flex squeezes them
        ? xs.slice(0, Math.max(0, rows - tall))
        : [...xs.slice(0, Math.max(0, rows - 1 - tall)), <Text key={`${key}-more`} color={dim} wrap="truncate-end">{`… ${xs.length - Math.max(0, rows - 1 - tall)} more`}</Text>]
      return (
        <Box key={`${key}-body`} flexDirection="column" height={rows} overflow="hidden" flexShrink={0}>
          {shown}
        </Box>
      )
    }
    const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`
    const stepsDone = a ? a.steps.filter(s => s.done).length : 0
    const laneCount = (w: string) => laneNodes.filter(l => l.word === w).length
    type Panel = { key: string; name: string; what: string; label: string; caption: string; stat: string; frac?: number; rows: RenderElement[]; tall?: number; log?: boolean }
    const PANELS: Panel[] = [
      { key: 'task', name: planQ ? 'PLAN' : 'TASK', what: planQ ? 'awaits your yes' : 'the brief in focus', rows: [],
        label: planQ ? 'TO APPROVE' : a ? a.stage.toUpperCase() : live ? 'WORKING' : 'IDLE',
        caption: a ? `${a.id} · ${a.criteria.filter(c => c.checked).length} of ${a.criteria.length} criteria met` : `${v.today_done ?? 0} closed today`,
        stat: a && a.steps.length ? `${stepsDone}/${a.steps.length} steps` : '', frac: a && a.steps.length ? stepsDone / a.steps.length : undefined },
      { key: 'lanes', name: 'LANES', what: 'builders in flight', label: plural(laneNodes.length, 'lane').toUpperCase(), rows: lanesBody(R.lanes ?? 0),
        caption: ['building', 'waiting', 'parked', 'merged'].map(w => `${w} ${laneCount(w)}`).join(' · '),
        stat: `${laneNodes.reduce((t, l) => t + l.done, 0)}/${laneNodes.reduce((t, l) => t + l.total, 0)} steps` },
      { key: 'merge', name: 'MERGE', what: 'waiting → build', label: ln.merging.length ? `${ln.merging.length} WAITING` : 'CLEAR', rows: mergeBody(R.merge ?? 0),
        caption: `${ln.merging.length} waiting to merge · ${ln.closing.length} merged, close owed`, stat: `${ln.merging.length + ln.closing.length} to land` },
      { key: 'activity', name: 'TOOLS', what: "this session's calls", label: nLive ? `${nLive} RUNNING` : plural(recentTicks.length, 'call').toUpperCase(),
        rows: [...laneRows, ...(axis ? [axisRow] : [])],
        caption: activityRight, stat: `${spanMs / 60000} min` },
      { key: 'agents', name: 'AGENTS', what: 'subagents and worktrees', label: gLive ? `${gLive} RUNNING` : plural(trees.length, 'worktree').toUpperCase(), rows: agentRows,
        caption: [gLive ? `${gLive} running` : '', trees.length ? plural(trees.length, 'worktree') : '', `the last ${gSpan / 60000} min`].filter(Boolean).join(' · '),
        stat: plural(Math.max(0, nodes.length - 1), 'node') },
      { key: 'queue', name: 'QUEUE', what: 'up next', label: `${queueN} QUEUED`, rows: queueRows,
        caption: [...flowWords(v), `${queueN} queued`, inboxN ? `${inboxN} in the inbox${v.inbox_yours ? ` (${v.inbox_yours} yours)` : ''}` : ''].filter(Boolean).join(', '),
        stat: '' }, // the next is the table's first row: the caption keeps the room
      { key: 'cpu', name: 'CPU', what: 'against the goal', label: cpu && cpuNow > cpu.goal ? 'OVER GOAL' : `GOAL ${cpu?.goal ?? 10}%`, rows: cpuBody(R.cpu ?? 0),
        caption: cpu ? `${plural(cpuSessions.length, 'session')} · the last ${Math.round((cpuTotals.length * cpu.step_s) / 60)} min · ${cpu.cores} cores` : '',
        stat: `${cpuNow.toFixed(1)}%`, frac: cpu ? Math.min(1, cpuNow / Math.max(0.1, cpu.goal)) : undefined },
      { key: 'checks', name: 'CHECKS', what: 'the gates', label: failed.length ? `${failed.length} FAILING` : `${passed}/${checkList.length} PASS`, rows: checksBody(R.checks ?? 0),
        caption: checks ? `ran ${checks.at.slice(11, 16)} · slowest ${Math.round(Math.max(0, ...checkList.map(r => r.s)))} s` : '', stat: `${passed}/${checkList.length}`,
        frac: checkList.length ? passed / checkList.length : undefined },
      { key: 'sessions', name: 'SESSIONS', what: 'live here', label: `${fleet.filter(s => !s.error).length} LIVE`, rows: sessionRows,
        caption: `${fleet.filter(s => s.task).length} on a task · ${fleet.reduce((t, s) => t + (s.mail ?? 0), 0)} mail`, stat: '' },
      { key: 'history', name: 'HISTORY', what: 'what happened', label: back ? `AS OF ${shown.at(-1)?.time || '?'}` : 'LIVE', rows: [...histSpark, ...historyRows], log: true,
        caption: hist.length ? `${plural(shown.length, 'event')} since ${shown[0]?.time || '?'}` : plural(recent.length, 'event'),
        stat: `${shown.filter(ev => '✗✖'.includes(ev.mark)).length} failed` },
      { key: 'today', name: 'TODAY', what: 'closed per day, the gauges', label: `${v.today_done ?? 0} DONE`, rows: todayBody, tall: Math.max(0, R.today! - 1),
        caption: showDays ? 'the last 14 days' : 'context · spend · hooks · gates', stat: `${v.today_done ?? 0} today` },
    ]
    const sections: RenderElement[] = []
    let num = 0
    for (const s of PANELS) {
      const n = R[s.key] ?? 0
      if (!n) continue
      const color = panelAccent(++num)
      sections.push(
        <Box key={s.key === 'task' ? 'task-panel' : s.key} flexDirection="column" flexShrink={0}>
          {panelHead(ui, e, { key: s.key, n: num, name: s.name, what: s.what, label: s.label, color, width: W })}
          {s.key === 'task'
            ? <Box key="task" flexDirection="column" height={n} overflow="hidden" flexShrink={0}>{taskRows.slice(0, n)}</Box>
            : body(s.key, n, s.rows, s.tall ?? 0, s.log)}
          {L.gap ? panelFoot(ui, e, { key: s.key, caption: s.caption, stat: s.stat, color, width: W, frac: s.frac }) : null}
        </Box>,
      )
    }

    return (
      <Box flexDirection="column" key="fm-pane" paddingX={1}>
        {crestTop.el}
        {head}
        {L.gap ? <Box key="gap-head" height={1} flexShrink={0} /> : null}
        {sections}
        {L.gap ? <Box key="gap-controls" height={1} flexShrink={0} /> : null}
        {controls}
      </Box>
    )
  })
}
