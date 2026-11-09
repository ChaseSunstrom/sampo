// The `fm ui --json` view model (v1): one snapshot of a Sampo project, rendered by every surface.
export type FmStep = { n: number; text: string; done: boolean; current: boolean }
export type FmCriterion = { n: number; text: string; verify: string | null; checked: boolean }
export type FmItem = {
  id: string
  type: string
  tier: string
  title: string
  status: string
  waits?: string | null // why it can't run without the user ("plan approval", "pending approval")
  plan?: FmPlan // only while waits is "plan approval": what a yes approves
  steps_done?: number
  steps_total?: number
  age_days?: number
  source?: string | null // T-3664: who asked: user and steer are the user's own words
}
// T-3664: a task in a lane, which the queue leaves out
/** T-3701: % of the machine's cores per session (each a cgroup scope `sampo cpu` sampled), in `step_s` buckets, oldest
 * first, the newest last; `now` their sum in the newest bucket; `goal` the project's (sampo cpu goal, default 10) */
export type FmCpu = { goal: number; cores: number; step_s: number; now: number | null; sessions: { name: string; pct: number[] }[] }
export type FmLane = { id: string; type: string; tier: string; title: string; steps_done: number; steps_total: number; since?: string | null
  files?: string[] } // T-3701: the files its leases hold (at most 12), for the LANES picture's dots
/** T-3701: the merge train for the MERGE picture: whether one runs now (its lock held), the lanes landed in the last day
 * (ids, newest last, at most 12, landed_at when each landed, and how many), the lanes parked (a conflict or a refusal) in that day */
export type FmTrain = { running: boolean; landed: string[]; landed_at?: string[]; landed_24h: number; parked: number; branch?: string }
/** T-3701: who called which model in the last five minutes (calls n, hot: in the last minute), for the MODELS picture */
export type FmFlow = { from: string; to: string; n: number; hot: number }
/** T-3701: the live page Board's slow parts (cache/board.json, read again at most every 30 s) for the pane's LIBRARY,
 * SAMPO, STATE, TESTS and CPU panels; each part null when it couldn't be read */
export type FmBoard = {
  at?: string
  library?: { calling: number; hourly: number[]; recent: { grid: string; at: string; kept: number }[] } | null
  counts?: { cards: number; ui: number; moons: number; calling: number } | null
  state?: { tree: { name: string; dir: boolean; at: string; n: number; kb: number }[]; newest: string;
    doc: { kind: string; title: string; text: string; at: string } | null } | null
  tests?: { modules: { name: string; state: 'running' | 'fail' | 'cached' | 'idle' }[]; done: number; total: number; running: number; source: string } | null
  commands?: { at: string; cmd: string; ok: boolean }[] | null
  /** the machine's cores by kind now, and a row a minute of the last 30 ([minute epoch, cores per kind…]), for CPU */
  cpu?: { kinds: string[]; goal_pct: number; cores: number; minutes: number[][]; now: Record<string, number> | null } | null
}
export type FmPlan = { interpretation: string; approach: string; steps: FmStep[]; criteria: FmCriterion[] }
export type FmActive = FmItem & {
  stage: string
  stages: string[]
  steps: FmStep[]
  criteria: FmCriterion[]
  audits: { done: number; need: number }
  blockers: string[]
  splits?: { stage: string; secs: number; typical: number | null; delta: number | null; sign: 'over' | 'under' | 'even' | null }[] // T-1078: time in each stage against the project's typical
  children?: { id: string; title: string; status: string; steps_done: number; steps_total: number }[] // T-0711
  on_task_s?: number | null // seconds since the task was first focused, when the view was built
  words?: string // T-3701: the raw request, unquoted (else the title): THE LOOP's "you asked"
  hypotheses?: FmHypothesis[] // T-0207/T-0228: the debugging ledger
  oracle?: { examples: number; ambiguities: string[] } | null // T-0226: examples from the spec alone
  batch?: string[] // T-0257: the requests this task works as one batch
  inconclusive?: number // T-0255: runs recorded as proving nothing (never counted)
  unverified?: string[] // T-0254: assumptions not tagged [verified: …], the first five
}
export type FmHypothesis = { n: number; status: 'open' | 'ruled out' | 'confirmed'; text: string }
// T-0228: what fm budget, fm bench/evolve and fm research ask add
export type FmBudget = {
  today_usd: number
  subagent_tokens: number
  caps: { day: number; run: number }
  halved: string | null
  subagents_paused?: string | null // T-0320: why subagents wait (usage ahead of pace)
  top: { feature: string; usd: number; runs: number; tokens: number }[]
}
export type FmBench = {
  cases: number
  last: { label: string; passed: number; total: number; cost_usd: number; at?: string | null; score?: number; repeats?: number } | null
  evolve: { kept: boolean | null; branch: string | null; target: string | null; why: string | null; at?: string }[]
  verdicts?: { kind: string; ok: boolean | null; verdict: string | null; at?: string }[] // T-0280: fm bench duel / versions
}
export type FmResearchAsk = { name: string; claims: number; verified: number; not_found: number; unchecked: number; at?: string; conflicts?: number; single?: number }
// T-0939: recent = newest first, a name once (at most 8); counts = uses per name over 7 days
export type FmSkills = {
  recent: { name: string; kind: 'skill' | 'card'; at: string }[]
  counts: Record<string, number>
  suggested: string[] // the repo map's stack cards
  unread: string[] // suggested cards not read this week
}
export type FmApproval ={ task: string; allow: string[]; why: string }
export type FmView = {
  v: number
  project: string | null
  root?: string
  mode?: { autonomy: string; drive: boolean; sensitive: boolean; trust?: boolean; risk_off?: string | null; standing?: string[] }
  active?: FmActive | null
  next?: string | null
  // T-2118: the Genesis tower's next action (fmgenesis.step, cached); kind ok|blocked|fatal|stopped|done
  genesis?: { stage: string; gen: number; action: string; command: string; kind: string } | null
  queue?: FmItem[] // the first 20
  queue_total?: number // T-3664: all of it
  inbox?: FmItem[] // the first 10, the user's own asks first
  inbox_total?: number
  inbox_yours?: number // T-3664: captured from the user's words (source user or steer)
  // T-3664: open work in lanes: a builder at it, finished and waiting on a merge, merged with its close owed
  lanes?: { building: FmLane[]; merging: FmLane[]; closing: FmLane[] }
  cpu?: FmCpu | null // T-3701: the Sampo sessions' CPU against the goal, from sampo cpu's state/cpu.json
  train?: FmTrain | null // T-3701
  flows?: FmFlow[] | null // T-3701
  board?: FmBoard | null // T-3701
  approvals?: FmApproval[]
  closed?: { id: string; status: string; title?: string; grade?: string | null; lenses?: string[] }[] // T-0473
  signals?: { level: 'red' | 'amber'; text: string }[] // T-0472: shown as one glyph only when any
  // T-0711: live sessions here and the cached tiles of remotes the user added (fm conductor remote add)
  fleet?: { session: string; project?: string | null; task?: string | null; title?: string | null; context_pct?: number | null
    model?: string | null; seen?: string | null // T-3701: the SESSIONS panel
    mail?: number; leases?: number; remote?: string; age_s?: number | null; error?: string }[]
  recent?: string[]
  skills?: FmSkills // T-0939: Skill calls and stack-card reads this week, and the map's unread suggestions
  health?: { hook_p95_ms: number | null; guard_blocks: number; hook_errors: number; paused_hooks?: string[] }
  watch?: string[] // paths whose mtime moves when the record changes (busy: at most one fm ui per 10 s)
  watch_now?: string[] // T-3580: what the user acts on (meta, asks, trust, pause, the active brief): a move reruns fm ui at once
  cached?: boolean // T-3542: fm printed its kept view (up to 10 s old); absent on a fresh build
  latency?: number[] // the latest hook run times (ms), oldest first
  palette_custom?: Record<string, string> // T-1717: fm design tokens --to pane --save, checked: C's keys, #rrggbb
  weekly_lean?: boolean // T-3049: false: weekly usage never puts this project in economy mode
  checks?: FmChecks | null // the last fm check run
  today_done?: number // tasks closed as done today
  learned?: string | null // T-1045: what Sampo learned this week (fixes, rules, interventions per closed task)
  look?: string | null // T-1672: the look of the day, rolled with the taste ledger's lean (null until it has taste)
  blocked_total?: number // T-1026: tasks blocked, for the work bar
  done_days?: number[] // T-0909: tasks closed per day, the last 14 days, today last
  brainstorm?: FmBrainstorm | null // the newest brainstorm (T-0124)
  loop?: { on: boolean; round: number; dry: number } | null // the forever loop (T-1057)
  worktrees?: FmWorktree[] | null // T-1023: git worktrees beside the main checkout
  history?: FmHistory[] | null // T-1026: what happened, structured, newest last
  budget?: FmBudget | null // T-0228: today's spend on child runs and subagents
  unproven?: { n: number; top: string | null } // T-2395: done tasks no run proved, and the kind most need
  bench?: FmBench | null // T-0228: bench cases, the newest run, evolve generations
  research?: FmResearchAsk[] // T-0228: the newest fm research ask notes
  revisit?: { date: string; decision: string; why: string }[] // T-0247: decisions whose revisit trigger fired
  vetoes?: string[] // T-0251: the user's recorded 'never/don't' corrections, newest first (checked before matching calls)
  risk_files?: string[] // T-3752: where /fm-risk off writes its record: everywhere's, then this project's
  trust_file?: string // where /fm-trust on writes the trust record (Sampo state)
  /** T-0145: a driven turn ended so this session's mod could reload; the reloaded mod starts the next turn */
  resume_after_reload?: { session: string; at: string; task?: string } | null
  typical?: Record<string, number> // median minutes focus → done per "TYPE/TIER" (3+ closed tasks)
}
export type FmHistory = { time: string; mark: string; task: string | null; text: string; title: string | null } // T-1026
// A brainstorm while fm ideas runs (answers in of expected) or after (count, the first ideas).
// T-1023: a git worktree: its branch, the task whose lane it is, the subagent it was made for (worktree-agent-<id>), its
// age and the commits it holds that HEAD doesn't
export type FmWorktree = { path: string; branch: string; task: string | null; agent: string | null; age_s: number | null; ahead: number | null; stopped?: boolean; open?: boolean }
export type FmBrainstorm = { name: string; running: boolean; answers: number; expected?: number; count: number; ideas: string[]; age_h?: number; grounded?: boolean }
export type FmCheck = { cmd: string; exit: number; s: number; note?: string | null }
export type FmChecks = { at: string; results: FmCheck[] }

// Live data the mod gathers itself from the session's tool calls.
export type FileChurn = { path: string; add: number; del: number; edits: number }
export type LiveAgent = {
  id: string
  type: string
  description: string
  startedAt: number
  lastAt?: number // its latest tool call
  tools: number
  last: string
  done: boolean
  calls?: number[] // T-1023: when each of its tool calls ran, for the Agents graph
}
// A background shell Claude started (or ctrl+b moved there), until its notification or a stop.
export type LiveShell = { id: string; command: string; startedAt: number }
export type Ctx = { percent: number }

// What one turn did, for the line that closes it (matched by its duration).
export type TurnSummary = { durationMs: number; tools: number; edits: number; add: number; del: number; agents: number; step: string }

declare module 'claude-code' {
  interface PluginState {
    'sampo-ui': {
      view: FmView | null
      error: string | null
      frame: number
      summaries: TurnSummary[]
      files: FileChurn[]
      agents: LiveAgent[]
      ctx: Ctx | null
      sound: boolean
      beat: number
      shells: LiveShell[]
      away: string[]
      resumed: string | null // the resume record (its `at`) this session already acted on
      reviewing: string | null // T-0989: the waiting plan shown in the Task section, by id
      scrub: number // T-1081: how many history entries back the pane is showing
      page: 'board' | 'details' // T-3701: the pictures, or every section (kept in the store, so a session starts on it)
    }
  }
}
