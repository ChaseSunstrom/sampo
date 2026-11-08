import type { RenderInput } from 'claude-code'

// T-0130: what the hooks, the rows and the pane share: constants, two timing maps and the settings. Atoms stay
// in register.tsx: the engine reads an atom only where it is declared.

export type ToolUseRender = RenderInput<'ToolUse'>

export const PANE = 'foreman'

export const OUT_LINES = 12 // a finished command's row: this many output lines (failures first, else the tail)
export const DIFF_LINES = 8 // a finished edit's row: this many changed lines
export const FRAME_MS = 120
export const IDLE_FRAMES = Math.round((15 * 60 * 1000) / FRAME_MS) // a lost turn.complete stops the clock after 15 min
export const QUIET_MS = 2 * 60 * 1000 // a running subagent with no tool call this long shows how long it has been quiet
export const STOP_MS = 10 * 60 * 1000 // and from here offers Stop
export const SHELL_MAX_MS = 2 * 3600 * 1000 // a background command runs at most 2 h; past that its notice was lost
export const CHECKPOINT_AT = 85 // context percent at which Sampo checkpoints once, so a compaction resumes exactly
export const FRESH = [
  'Sampo task boundary: a task just closed.',
  "Keep: the user's standing requests and preferences, decisions still in force, and what the next queued task needs.",
  "Drop: finished tasks' file contents, diffs, logs and back-and-forth.",
  "Sampo's record (fm state, the briefs) is the source of truth for tasks: after this, check `fm next` and re-read",
  'files before editing them, since what was read earlier may be out of date.',
].join(' ')

/** What changed between two snapshots that deserves a toast. */

export const starts = new Map<string, number>() // tool_use_id → when it started (ms)
export const took = new Map<string, number>() // tool_use_id → how long it ran (ms), for the finished row

/** userConfig and the session's folder, set by register (rows read them). */
export const cfg = { freshAt: 40, freshTokens: 200000, mascot: 'sampo', root: '', motion: 'full' as 'full' | 'reduced' | 'off',
  chat: 'tree' as 'tree' | 'full', palette: 'default' as 'default' | 'forge' | 'cb' | 'riso' }
/** T-1040: in the tree chat a finished call shows this much: a passing command's output only when it is this short,
 * this many changed lines of a diff */
export const TINY_OUT = 2
export const DIFF_TREE = 4
export const diffCap = (tree: boolean) => (tree ? DIFF_TREE : DIFF_LINES) // the caller passes treeOn(e): terminal only

// T-0909: what the pane charts live. Every tool call's lane and start (the swim lanes, the last hour), and each call
// still running with how it reads (the "now" card); neither needs to survive a reload.
export type Lane = 'edit' | 'run' | 'read' | 'agent' | 'skill' | 'web'
export const ticks: { at: number; lane: Lane; what?: string }[] = []
export const running = new Map<string, { icon: string; verb: string; target: string; color: number; at: number; agent: boolean }>()
// T-1033: the band's live tree: every call (the session's own, agent null, and each subagent's) with how it ended
export type FeedItem = { id: string; agent: string | null; icon: string; verb: string; target: string; color: number
  at: number; end?: number; ok?: boolean }
export const feed: FeedItem[] = []
// T-1090: what each node of the live tree last said (a model step's first line), keyed by subagent id ('' for this
// session): the newest three, the one still streaming marked live
export type Said = { text: string; at: number; live: boolean }
export const said = new Map<string, Said[]>()
export const TREE_MS = 2 * 60 * 1000 // the band's full tree stays this long after the last call
export const laneOf = (icon: string): Lane =>
  icon === '✎' ? 'edit' : icon === '❯' ? 'run' : icon === '◆' ? 'agent' : icon === '✦' ? 'skill' : icon === '◍' || icon === '⬡' ? 'web' : 'read'
export const TIMELINE_MS = 30 * 60 * 1000 // the lanes' window
