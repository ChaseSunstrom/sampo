import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderElement, RenderInput, ResolveInput } from 'claude-code'


const scrub = atom({ plugin: 'foreman-ui', key: 'scrub' } as const, 0) // T-1081: how many history entries back the pane is showing
import type { FileChurn, FmItem, FmView, LiveAgent, LiveShell, TurnSummary } from '../types'
import {
  C,
  cleanDeep,
  setPalette,
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
} from './kit'
import { CHECKPOINT_AT, FRAME_MS, FRESH, IDLE_FRAMES, PANE, QUIET_MS, SHELL_MAX_MS, STOP_MS, TIMELINE_MS, TREE_MS, cfg, feed, laneOf, running, said, starts, ticks, took } from './state'
import type { FeedItem } from './state'

const view = atom({ plugin: 'foreman-ui', key: 'view' } as const, null)
const PANE_MIN = 26 // T-1679: the narrowest pane the layout fits (24 inside the padding); below it, one line
const error = atom({ plugin: 'foreman-ui', key: 'error' } as const, null)
const frame = atom({ plugin: 'foreman-ui', key: 'frame' } as const, 0)
const summaries = atom({ plugin: 'foreman-ui', key: 'summaries' } as const, [])
const files = atom({ plugin: 'foreman-ui', key: 'files' } as const, [])
const agents = atom({ plugin: 'foreman-ui', key: 'agents' } as const, [])
const ctx = atom({ plugin: 'foreman-ui', key: 'ctx' } as const, null)
const sound = atom({ plugin: 'foreman-ui', key: 'sound' } as const, true)
const beat = atom({ plugin: 'foreman-ui', key: 'beat' } as const, 0)
const shells = atom({ plugin: 'foreman-ui', key: 'shells' } as const, [])
const away = atom({ plugin: 'foreman-ui', key: 'away' } as const, []) // tasks done since the person last wrote
const resumed = atom({ plugin: 'foreman-ui', key: 'resumed' } as const, null) // T-0145: the resume record acted on
const reviewing = atom({ plugin: 'foreman-ui', key: 'reviewing' } as const, null as string | null) // T-0989: a waiting plan shown in the Task section
import { LIGHT, TREE_INDENT, agentOut, agentRow, bars, branch, editChanges, editRow, gapTop, crestTree, liveTree, mascotTree, meter, miniRail, ownsResult, rail, railBox, sectionHead, shellRow, spark, toolRow, treeOn } from './rows'
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
  palette: v => v === 'default' || v === 'cb' || v === 'riso',
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
    const m = /^foreman-ui(?:@[^.]+)?\.(.+)$/.exec(r.key)
    if (m) now[m[1]!] = r.value
  }
  const seen = JSON.stringify(now)
  if (seen === seenOptions) return
  seenOptions = seen
  const { out, kept } = usable({ ...startOptions, ...now }, goodOptions)
  applyOptions(out)
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

async function openPane($: EngineInterface, focus: boolean) {
  // ~76 columns docked: the cards fit, and the transcript and band keep the rest (T-0122: the share left them ~37)
  return $.ui.open(focus ? { id: PANE, title: 'Sampo', focus: true, columns: 76 } : { id: PANE, title: 'Sampo', columns: 76 })
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
  cfg.palette = options.palette === 'cb' || options.palette === 'riso' ? options.palette : 'default' // T-1081, T-1512
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
        await $.prompt.suggest({ text: '/foreman:next' }).catch(() => undefined)
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
    return text ? { ...r, sections: [...r.sections, { id: 'foreman-ui:economy', text, scope: 'session' }] } : r
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
      return tree(
        <Box flexDirection="row" gap={1} key="fm-read">
          <Text color={hex(face.color)}>{face.icon}</Text>
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
    const ours = /^The foreman-ui plugin sent a message:\n/ // how the model reads it; the row may carry that text
    if (((origin?.kind === 'plugin' && origin.name === 'foreman-ui') || ours.test(e.props.text)) && !e.props.isExpanded) {
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
          <Box flexDirection="row" gap={1} key="fm-band-queue-row">
            {!narrow && <Text color={hex(C.dim)}>today</Text>}
            {!narrow && meter($.ui.resolve(e), e, 'fm-band-queue', 10, todayDone / Math.max(1, todayDone + queued + inboxN + (a ? 1 : 0)), C.accent2, null)}
            <Text color={hex(C.dim)} wrap={cut}>
              {[`✓${todayDone} done`, ...inFlight, `${queued} queued`, `${inboxN} in inbox`].join(' · ')}
              {upNext && !narrow ? ` · next ${upNext.id} ${upNext.title}` : ''}
            </Text>
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
              onPress={() => void $.prompt.submit({ text: '/foreman:next', asUser: true })}
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
          <Text color={hex(C.dim)}>Ask Claude to set it up, or run /foreman:intake with a request.</Text>
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
    const typeWord = (it: { type: string }) => (
      <Box flexShrink={0}>
        <Text weight="bold" color={typeColor(it.type)}>
          {(it.type === 'PERFORMANCE' ? 'PERF' : it.type).padEnd(8)}
        </Text>
      </Box>
    )

    // ── up next: plans waiting for a yes first, the queue, the inbox, a line to capture into it
    const capture = captureBox($, e)
    const ids = (xs: { id: string }[], n = 6) => xs.slice(0, n).map(x => x.id).join(' ') + (xs.length > n ? ` +${xs.length - n}` : '')
    const queueRows: RenderElement[] = [
      // T-3664: what is moving first: builders at work, then the lanes waiting on a merge or a close
      ...ln.building.map(l => (
        <Box flexDirection="row" gap={1} key={`b-${l.id}`}>
          <Box flexShrink={0}>
            <Text color={hex(C.agent)}>⚙</Text>
          </Box>
          {typeWord(l)}
          <Box flexShrink={0}>
            <Text color={hex(C.agent)}>{l.id}</Text>
          </Box>
          <Text wrap="truncate-end">{l.title}</Text>
          {l.steps_total > 0 && (
            <Box flexShrink={0}>
              <Text color={dim}>{`${l.steps_done}/${l.steps_total}`}</Text>
            </Box>
          )}
        </Box>
      )),
      ...(ln.merging.length ? [<Text key="b-merging" color={muted} wrap="truncate-end">{`⇢ ${ln.merging.length} waiting to merge · ${ids(ln.merging)}`}</Text>] : []),
      ...(ln.closing.length ? [<Text key="b-closing" color={muted} wrap="truncate-end">{`✓ ${ln.closing.length} merged, close owed · ${ids(ln.closing)}`}</Text>] : []),
      ...[...queue].sort((x, y) => Number(!!y.plan) - Number(!!x.plan)).map((q, i) => (
        <Box flexDirection="row" gap={1} key={`q-${q.id}`}>
          <Box flexShrink={0}>
            <Text color={hex(i === 0 ? C.accent2 : C.track)}>{i === 0 ? '▶' : '·'}</Text>
          </Box>
          {typeWord(q)}
          <Box flexShrink={0}>
            <Text color={hex(q.waits ? C.warn : C.accent)}>{q.id}</Text>
          </Box>
          <Text wrap="truncate-end" color={q.waits ? hex(C.warn) : undefined}>
            {q.title}
          </Text>
          {q.plan && (
            <Button key={`approve-${q.id}`} label="✓ approve" plain onPress={() => act($, ['task', 'set', q.id, 'approved=true'], `✓ ${q.id} plan approved`)} />
          )}
        </Box>
      )),
      ...inbox.map(it => (
        <Box flexDirection="row" gap={1} key={`in-${it.id}`}>
          <Box flexShrink={0}>
            <Text color={hex(it.source === 'user' || it.source === 'steer' ? C.accent : (it.age_days ?? 0) >= 7 ? C.warn : C.track)}>⚑</Text>
          </Box>
          {typeWord(it)}
          <Box flexShrink={0}>
            <Text color={muted}>{it.id}</Text>
          </Box>
          <Text wrap="truncate-end" color={hex(mix(C.dim, 0xc8ccd4, 0.7))}>
            {it.title}
          </Text>
          <Button key={`start-${it.id}`} label="start" plain dimColor
            onPress={() => void $.prompt.submit({ text: `Plan and start ${it.id} (/foreman:intake).`, asUser: true })} />
          <Button key={`drop-${it.id}`} label="drop" plain dimColor
            onPress={() => act($, ['task', 'drop', it.id, 'dropped from the Sampo pane'], `${it.id} dropped`)} />
        </Box>
      )),
      ...(inboxN > inbox.length ? [<Text key="in-more" color={dim}>{`… ${inboxN - inbox.length} more in the inbox`}</Text>] : []),
      ...(capture ? [capture] : []), // last: a crowded dock cuts it before a plan waiting for a yes
    ]

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
    // T-3113: the crest takes the pane's top rows; every section is placed in what is left under it
    const mood = live ? 'work' : clockNow < happyUntil ? 'happy' : 'idle'
    const crestTop = crestTree(ui, e, W, H, mood, live ? (f ?? 0) : idleBeat, subs)
    const L = layout(H - crestTop.rows, [
      { key: 'task', want: taskWant, cap: Math.max(4, Math.round(H * 0.4)), min: Math.min(taskWant, 4), bare: true },
      { key: 'activity', want: 1, fixed: nLanes + axis, grow: KINDS.length - nLanes }, // spare rows: every lane
      { key: 'today', want: 1, fixed: T, grow: Math.max(0, 8 - T) }, // then taller charts
      { key: 'agents', want: nodes.length, cap: Math.max(3, Math.round(H * 0.18)), min: Math.min(nodes.length, H >= 40 ? 5 : 3) }, // the root and its children, not just the root
      { key: 'queue', want: queueRows.length, cap: Math.max(3, Math.round(H * 0.2)), min: plans.length + 2 }, // every plan's approve
      { key: 'sessions', want: sessionRows.length, cap: 5, min: Math.min(sessionRows.length, 3) },
      { key: 'history', want: historyRows.length, stretch: true },
    ])
    const R = L.rows

    // ── the one thing that needs you
    const attention = (() => {
      const row = (color: number, text: string, lead?: RenderElement | null, tail?: RenderElement | null) => (
        <Box key="fm-pane-attention" flexDirection="row" gap={1} height={1} overflow="hidden" flexShrink={0}>
          {lead}
          <Text color={hex(color)} wrap="truncate-end">
            {text}
          </Text>
          {tail}
        </Box>
      )
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
        return row(
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

    // ── head: who and how, the mascot on the right
    const modes: [string, number][] = m
      ? [
          [m.autonomy === 'full' ? 'full auto' : 'standard', m.autonomy === 'full' ? C.accent2 : C.dim],
          ...(m.drive ? [] : ([['drive off', C.warn]] as [string, number][])),
          ...(m.sensitive ? ([['sensitive', C.warn]] as [string, number][]) : []),
          ...(m.trust ? ([['trust on', C.warn]] as [string, number][]) : []),
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

    // ── drawing: a titled divider, then exactly the section's rows; what doesn't fit is counted, never cut silently
    // T-1026: a divider is a line in the section's own colour, fading out; the words around it read in muted, not dim
    const rule = (key: string, width: number, color: number) => {
      const n = Math.max(1, width)
      return term
        ? <Raster key={key} columns={n} rows={1} cells={ruleCells(n, color)} />
        : <Text key={key} color={hex(mix(C.track, color, 0.5))}>{'─'.repeat(n)}</Text>
    }
    const titleRow = (key: string, label: string, color: number, right: string) => (
      <Box key={`${key}-title`} flexDirection="row" gap={1} height={1} overflow="hidden">
        <Box flexShrink={0}>
          <Text weight="bold" color={hex(color)}>{label}</Text>
        </Box>
        <Box flexShrink={0}>{rule(`${key}-rule`, W - label.length - 1 - (right ? right.length + 1 : 0), color)}</Box>
        {right ? (
          <Box flexShrink={0}>
            <Text color={muted}>{right}</Text>
          </Box>
        ) : null}
      </Box>
    )
    const todayTitle = (
      <Box key="today-title" flexDirection="row" height={1} overflow="hidden">
        {showDays && <Box flexShrink={0}><Text weight="bold" color={hex(C.ok)}>Closed per day </Text></Box>}
        {showDays && <Box flexShrink={0}>{rule('days-rule', leftW - 15, C.ok)}</Box>}
        {showDays && <Box flexShrink={0} width={GAP} />}
        <Box flexShrink={0}><Text weight="bold" color={hex(C.accent2)}>Today </Text></Box>
        <Box flexShrink={0}>{rule('today-rule', W - leftW - GAP - 6, C.accent2)}</Box>
      </Box>
    )
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
    const SECTIONS: { key: string; title: RenderElement; rows: RenderElement[]; tall?: number; log?: boolean }[] = [
      { key: 'activity', title: titleRow('activity', 'Activity', nLive ? C.accent : C.dim, activityRight), rows: [...laneRows, ...(axis ? [axisRow] : [])] },
      { key: 'agents', title: titleRow('agents', 'Agents', gLive ? C.agent : C.dim,
          [gLive ? `${gLive} running` : '', trees.length ? `${trees.length} worktree${trees.length === 1 ? '' : 's'}` : '', `${gSpan / 60000}m`].filter(Boolean).join(', ')),
        rows: agentRows },
      { key: 'queue', title: titleRow('queue', 'Up next', C.accent2, [...flowWords(v), `${queueN} queued`, inboxN ? `${inboxN} in the inbox${v.inbox_yours ? ` (${v.inbox_yours} yours)` : ''}` : ''].filter(Boolean).join(', ')), rows: queueRows },
      { key: 'sessions', title: titleRow('sessions', 'Sessions', C.agent, `${fleet.filter(s => !s.error).length} live`), rows: sessionRows },
      { key: 'history', title: titleRow('history', 'History', C.dim, ''), rows: historyRows, log: true },
      { key: 'today', title: todayTitle, rows: todayBody, tall: Math.max(0, R.today! - 1) },
    ]
    const sections: RenderElement[] = []
    for (const s of SECTIONS) {
      const n = R[s.key] ?? 0
      if (!n) continue
      if (L.gap) sections.push(<Box key={`gap-${s.key}`} height={1} flexShrink={0} />)
      sections.push(
        <Box key={s.key} flexDirection="column" flexShrink={0}>
          {s.title}
          {body(s.key, n, s.rows, s.tall ?? 0, s.log)}
        </Box>,
      )
    }

    return (
      <Box flexDirection="column" key="fm-pane" paddingX={1}>
        {crestTop.el}
        {head}
        {L.gap ? <Box key="gap-head" height={1} flexShrink={0} /> : null}
        <Box key="task" flexDirection="column" height={R.task!} overflow="hidden" flexShrink={0}>
          {taskRows.slice(0, R.task!)}
        </Box>
        {sections}
        {L.gap ? <Box key="gap-controls" height={1} flexShrink={0} /> : null}
        <Box flexDirection="row" gap={W >= 64 ? 3 : 2} key="fm-pane-controls" height={1} overflow="hidden" flexShrink={0}>
          <Button key="next" label="next" hotkey="n" plain onPress={() => void $.prompt.submit({ text: '/foreman:next', asUser: true })} />
          <Button key="drive" label={m?.drive ? 'turn drive off' : 'turn drive on'} hotkey="d" plain
            onPress={() => act($, ['drive', m?.drive ? 'off' : 'on'], `drive ${m?.drive ? 'off' : 'on'}`)} />
          <Button key="autonomy" label={m?.autonomy === 'full' ? 'switch to standard' : 'switch to full auto'} plain dimColor
            onPress={() => {
              const level = m?.autonomy === 'full' ? 'standard' : 'full'
              return act($, ['autonomy', level], `autonomy ${level}`)
            }} />
          <Button key="sound" label={isOn ? 'mute' : 'unmute'} plain dimColor
            onPress={async () => {
              await update($, sound, x => !x)
              await $.store.set('sound', await read($, sound))
            }} />
 <Button key="scrub-back" label="◂ back" hotkey="b" plain dimColor onPress={() => update($, scrub, x => x + 1)} />
          <Button key="scrub-live" label="live" hotkey="l" plain dimColor onPress={() => update($, scrub, () => 0)} />
          <Button key="refresh" label="refresh" hotkey="r" plain dimColor onPress={() => refresh($)} />
        </Box>
      </Box>
    )
  })
}
