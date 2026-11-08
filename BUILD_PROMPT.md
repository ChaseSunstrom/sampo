# FOREMAN — Build Prompt for Claude Code

> **How to use this file:** it lives at the root of the user's `foreman` git repo, cloned to `~/.claude/foreman` by `install.sh` (which also runs `setup-plugins.sh` and installs the bootstrap plugin). Start the build with `/sampo:build` in Claude Code, or `install.sh --build`.
> Everything above Appendix A is addressed to you, the Claude Code agent. Appendix A records the plugin decisions for this machine; the executable version is `setup-plugins.sh`.

---

## 0. Settings (the user may edit these before you start)

```yaml
SYSTEM_NAME: foreman              # plugin name, slash-command namespace (/foreman:*); CLI is `fm`
FOREMAN_HOME: ~/.claude/foreman   # the user's git repo AND the one central location for code, docs, and state
AUTONOMY: standard                # guarded | standard | autopilot  (see §6.6)
BUILD_GATE: on                    # on = stop once after the Phase 1 plan for approval; off = proceed
GIT: commit-only                  # commit to branch foreman/build; never push (see §2.11)
PERMISSIONS: bypass               # install.sh sets permissions.defaultMode=bypassPermissions (see §4.7)
```

---

## 1. Mission

Build **Foreman**: hooks, a small CLI, skills, always-on rules, and a couple of read-only subagents, packaged as one local Claude Code plugin, that make you run every request, however terse, through one disciplined loop:

**capture → expand → ground → plan → execute → verify → reflect → record**

with one central, always-current record of what you are doing, what is queued, what was decided, and what was learned, plus active hygiene so that record never rots.

The target is not "AGI." It is a senior engineer with a good project manager's discipline: never loses the thread, never ships half-done work, anticipates the obvious follow-up so the user doesn't have to ask for it, and never wanders off on a tangent.

**Success looks like this:**
- The user types `FIX: login times out on slow wifi` (or an untagged one-liner) and gets a grounded plan, execution in the right order, proof that it works, and a clean record, with no re-explaining next session.
- A new idea arriving mid-task gets captured and planned, and the current task still gets finished first.
- After `/compact`, `/clear`, a crash, or a new session, work resumes at the exact step.
- There is one source of truth per kind of information, no duplicate trackers, no competing workflows, and all of it is mapped in `MASTER.md`.
- It is cheap: small always-on context, fast hooks, can be disabled with one command and removed with one script.

---

## 2. Non-negotiables

1. **Verify before you build.** Your training data about Claude Code is stale. Before writing any hook, skill, plugin manifest, subagent, rule, or settings change, read the current docs (index: `https://code.claude.com/docs/llms.txt`; at minimum: hooks, hooks-guide, skills, plugins + plugin reference, sub-agents, memory, settings, statusline, claude-directory) and run `claude --version`. Where the docs and this file disagree, the docs win; record the discrepancy in `PLAN.md`.
2. **Don't break what exists.** Back up first. Never edit files owned by other plugins or by managed policy. Merge settings, never overwrite them. Everything you add must be removable by `uninstall.sh`.
3. **One writer.** The main thread is the only writer of code and state. Subagents are read-only (§8).
4. **All state writes go through `fm`** (atomic write + lock), never ad-hoc `echo >>`. Hooks for the same event run in parallel; this is how races are avoided.
5. **Evidence or it didn't happen.** No task or step is done without recorded verification evidence (command + result).
6. **Proportionality.** Planning depth scales with task size (§6.2). A typo does not get a design doc; nothing gets zero thought.
7. **Instructions vs. context.** Imperative instructions live in rules and skills. Anything a hook injects (`additionalContext` / stdout) is short *factual state* ("Active task: T-0012 [FIX] step 3/5. Inbox: 2."), never commands; imperative hook text can trip prompt-injection defenses.
8. **Budgets.** Always-on instruction footprint added by Foreman ≤ ~120 lines. SessionStart injection ≤ 2,000 chars. Per-prompt injection ≤ 400 chars. Per-tool-call hooks p95 ≤ 150 ms. SessionEnd work must fit its ~1.5 s budget. No `prompt`/`agent` hook types on hot paths.
9. **Fail safe.** Non-guard hooks fail open (exit 0, error logged to `FOREMAN_HOME/state/logs/hooks.log`). Guard hooks block deliberately (exit 2 or JSON deny; exit 1 never blocks) and fail *closed*. With bypass as the default permission mode (§4.7), the guard hook and deny rules are the only automatic brakes left, so they are mandatory and tested, not optional.
10. **No secrets in state.** Redact tokens, keys, and passwords from anything written to the ledger, briefs, or logs. No network calls from hooks.
11. **Git hygiene.** `FOREMAN_HOME` is the user's repo, and they push it to GitHub. Work on branch `foreman/build` in small conventional commits. Never push, never rewrite history, and never commit `state/`, `local/`, `backups/`, logs, machine inventories, or secrets (all gitignored; doctor verifies nothing ignored is tracked and scans staged files for secrets). Machine-specific outputs (`recon.md`, `PLAN.md`, `verification.md`) go in `local/`. Don't edit `BUILD_PROMPT.md` unless asked; record deviations in `local/PLAN.md` and `MASTER.md`. Keep `install.sh`, `setup-plugins.sh`, and `configure-repo.sh` working; extend them rather than replacing them.
12. **Third-party plugins are untrusted code.** In Phase 0, read the hook definitions and scripts of every non-Anthropic plugin and flag network calls, writes outside their own directories, settings edits, or transcript access. Report findings; never modify another plugin's files.

---

## 3. Build phases (do these in order)

**Phase 0: Recon and backup** (read-only except for the backup)
- Confirm `FOREMAN_HOME` is a git repo with this file at its root, then create and switch to branch `foreman/build` (or resume on it).
- Environment: `claude --version`, OS, shell, and availability of `git`, `jq`, `python3`, `flock`, and `node` on the *non-interactive* shell PATH (ponytail and claude-hud hooks need node).
- Read the docs listed in §2.1.
- Inventory: `~/.claude/settings.json` (+ project/local settings if inside a repo), every CLAUDE.md and `rules/` file, skills, commands, agents, `claude plugin list`, `claude plugin details <plugin>` for each (always-on token cost), every enabled plugin's `hooks/hooks.json`, `claude mcp list`, statusLine, output style, permission mode and allow/deny rules, auto memory status and location.
- Compare the installed set with §4.6 and Appendix A; `setup-plugins.sh --dry-run` gives a quick diff.
- If `~/.claude-reset/` exists, the user ran `reset-claude.sh` before the build. Inventory the newest archive: its `moved/` folder (their old CLAUDE.md, rules, skills, agents, commands, hooks) and the removed plugins inside `claude-home.tgz` (under `.claude/plugins/cache/`, which is where the ECC and superpowers material to port in §4.6 now lives). Propose what to bring back; restore nothing without approval.
- **Measure the always-on load** as the baseline Foreman must beat: CLAUDE.md files, `~/.claude/rules/`, each enabled plugin's always-on tokens (`claude plugin details`), and every hook that injects context at SessionStart/UserPromptSubmit (list them and estimate their size).
- Audit third-party plugin hooks per §2.12.
- Back up `~/.claude` to `FOREMAN_HOME/backups/<timestamp>.tgz`, excluding transcripts and caches (list exactly what was excluded; make sure auto-memory directories are included). Write and test the restore command.
- Output `FOREMAN_HOME/local/recon.md`: inventory; the load baseline; hook audit; conflicts (hooks on the same events, name collisions, competing memory or workflow systems); opportunities (which installed tool serves which lifecycle stage).

**Phase 1: Plan**
Write `FOREMAN_HOME/local/PLAN.md`, starting from the §4.6 defaults, containing: deltas from §4 with reasons; file tree; hook table (event, matcher/`if`, script, purpose, latency budget, fail mode, what it injects); `fm` CLI spec; schemas (brief frontmatter, ledger event, STATE format); integration map (lifecycle stage → installed tool → how invoked) and conflict decisions (keep / wrap / disable, with reasons); exact planned diffs to settings, CLAUDE.md, and rules; test plan mapped to §12; rollout order, rollback, uninstall; risks and mitigations; open questions, each with a default.
Self-critique the plan against §6.5 and revise (at most twice). If `BUILD_GATE: on`, stop and present a ≤ 30-line summary plus the batched questions with defaults. Otherwise proceed.

**Phase 2: Core.** `fm` CLI, state layout, templates, unit tests. Then register the Foreman build itself as a Foreman project and load Phases 3–8 into its queue as tasks. **From here on, dogfood Foreman.**

**Phase 3: Hooks.** One dispatcher per event, fixture-payload tests, latency measurements. The Foreman plugin is installed from this repo as a local-directory marketplace, which loads in place: edits under `plugin/` take effect on `/reload-plugins` or the next session. Use `claude --plugin-dir <path>` for isolated tests (e.g. scratch repos with `claude -p`), and `claude plugin validate --strict` after every manifest change.

**Phase 4: Skills, commands, rules, agents, visibility.** `rules/foreman.md`, the skills in §4.5, the read-only agents in §8, and the visibility layer in §4.8.

**Phase 5: Integration.** Apply the approved §4.6 decisions: wire plugins into the lifecycle, disable conflicts (never uninstall without asking), port the procedures listed in §4.6 with attribution, apply the CLAUDE.md changes from §10, finish the permission safety net in §4.7. Foreman is already installed as `sampo@sampo` (the bootstrap); extend `plugin/`, keeping `commands/build.md` working.

**Phase 6: Hygiene and doctor.** `fm tidy` (§9) and `fm doctor` (§12).

**Phase 7: Verification.** Doctor green; all §12 scenarios pass; evidence recorded in `FOREMAN_HOME/local/verification.md`. Encode the scenarios as `claude plugin eval` cases in `plugin/evals/`; they become the referee for §4.9.

**Phase 8: Docs and handoff.** `MASTER.md` (§11, committed at the repo root), plugin README, `README.md` updated, uninstall tested (install → uninstall → reinstall leaves no residue), final commit on `foreman/build`, final report (§13) ending with the exact merge-and-push commands for the user.

---

## 4. Architecture (target; change it in PLAN.md only with reasons)

### 4.1 Layout

```
FOREMAN_HOME/                      # the user's git repo (pushed to GitHub)
├── README.md  install.sh  setup-plugins.sh  configure-repo.sh   # exist already; keep working
├── BUILD_PROMPT.md                # this file (origin spec)
├── MASTER.md                      # map of the whole system (§11), created in Phase 8
├── .claude-plugin/marketplace.json   # local marketplace "sampo" -> ./plugin (exists)
├── local/                         # gitignored: recon.md, PLAN.md, verification.md
├── plugin/                        # the plugin; already has .claude-plugin/plugin.json + commands/build.md
│   ├── .claude-plugin/plugin.json
│   ├── hooks/hooks.json           # ONE dispatcher per event
│   ├── hooks/*                    # dispatcher scripts
│   ├── bin/fm                     # the only writer of state
│   ├── skills/                    # see §4.5
│   ├── agents/                    # fm-recon (+ optional fm-reviewer), read-only
│   ├── rules/foreman.md           # always-on rules; install.sh symlinks into ~/.claude/rules/
│   ├── templates/                 # brief, STATE, CLAUDE.md
│   ├── tests/                     # fixtures, doctor checks, e2e scenarios
│   └── uninstall.sh
├── backups/                       # gitignored
└── state/                         # gitignored runtime data
    ├── registry.md                # project slug → path, last active
    ├── logs/
    └── projects/<slug>/
        ├── STATE.md               # focus + next action, ≤ 60 lines, regenerated by fm
        ├── INBOX.md               # captured, not yet planned
        ├── tasks/T-0001-<kebab>.md
        ├── decisions.md           # date, decision, why, alternatives rejected
        ├── research/
        ├── ledger.jsonl           # append-only event log; never loaded wholesale
        └── archive/
```

`slug` = repo directory name + short hash of the absolute git root (collision-safe). Keep state in `FOREMAN_HOME/state` rather than the plugin data directory so everything lives in one findable place and survives reinstalls.

### 4.2 Single source of truth (put this table in MASTER.md and enforce it)

| Information | Lives in | Loaded into context |
|---|---|---|
| Foreman operating rules | `~/.claude/rules/foreman.md` (symlink) | every session |
| User's stable personal preferences | `~/.claude/CLAUDE.md` | every session |
| Project conventions and commands | project `CLAUDE.md` | every session in that project |
| Path/topic-specific rules | `.claude/rules/*.md` with `paths:` | when matching files are read |
| Procedures (intake, execute, tidy, delegate) | skills | on demand |
| Current focus and next step | `STATE.md` | injected at startup/resume/clear/compact |
| Task specs, progress, evidence | `tasks/T-*.md` | on demand |
| Unplanned requests | `INBOX.md` | count + titles injected |
| Why things were decided | `decisions.md` | on demand |
| Durable learnings and gotchas | native auto memory (`MEMORY.md` + topic files) | startup window, then on demand |
| History | `ledger.jsonl` | never wholesale; query via `fm` |
| Knowledge graph (only if a graph/memory MCP exists) | that MCP | on demand |
| In-session checklist | built-in task tool | mirrors the active brief's steps only |

If a fact would live in two places, pick one and link from the other. Prefer native Claude Code mechanisms (auto memory, rules, built-in task tool) over reinventing them: Foreman orchestrates, it doesn't duplicate. If recon shows a native mechanism is disabled or unsuitable, propose the alternative in `PLAN.md`.

### 4.3 Hooks (verify every event name, matcher value, and field against current docs)

| Event (matcher) | Purpose | Blocks? |
|---|---|---|
| SessionStart (startup / resume / clear / compact) | register project; inject STATE summary: active task, current step, queue head, inbox count, "tidy overdue" flag | no |
| UserPromptSubmit | detect intake tags and override words; inject a 1–3 line factual note ("Message contains 2 intake items. Active: T-0012 step 3/5.") | no |
| PreToolUse (file-edit tools; Bash) | soft scope note vs. active brief; guard destructive commands outside the project and direct writes to state files | guard only |
| PostToolUse (file-edit tools) | record touched files in the ledger | no |
| PreCompact | `fm checkpoint`: flush the exact resume point into brief + STATE (never block compaction) | no |
| Stop | completion gate: if the active step claims done without evidence, block **once** with a factual reason; honor `stop_hook_active`; never loop | once |
| TaskCompleted | refuse to complete a mirrored built-in task whose brief step lacks evidence | yes |
| SubagentStop | ledger pointer to the subagent's summary | no |
| MessageDisplay | display-only per-reply badge and timestamp (§4.8); zero tokens, transcript untouched | no |
| Notification, Stop (async) | terminal title, taskbar progress, desktop notifications via `terminalSequence` (§4.8) | no |
| SessionEnd | trivial bookkeeping only (last-active); never tidy here | no |

Use handler `if` filters to avoid spawning processes needlessly. Heuristics run in the scripts; judgment happens in you, guided by the rules. **Don't replace claude-hud's statusline**; compose with it as §4.8 describes. Check every other plugin's hooks on the same events (especially security-guidance's Stop, SubagentStop, and UserPromptSubmit hooks, and ponytail's SessionStart/UserPromptSubmit/SubagentStart hooks) and document how they coexist.

### 4.4 The `fm` CLI

```
fm init [path]                    register project, create state, print slug
fm state [--brief] [--json]       print STATE (hooks use --json)
fm capture "<text>" [--source user|discovered|followup] [--type T] [--tier S|M|L]
fm task new|show|set|step|evidence|done|block|drop <id> ...
fm focus <id> | fm checkpoint [--note ...] | fm resume
fm queue [--replan]               ordered queue (canonical order + dependencies)
fm log <event> [json]
fm tidy [--apply]                 dry-run by default
fm doctor
```

Requirements: one implementation language chosen after recon (Python 3 stdlib-only, or POSIX sh + jq); `#!/usr/bin/env` shebangs; atomic writes (temp file + rename); a per-project lock with timeout; every mutating command appends a ledger event `{ts, session_id, project, task, event, data}`; `--json` output for hooks; documented exit codes; no network; tolerate two Claude Code sessions in the same project (lock, and warn if another live session holds focus).

### 4.5 Skills and commands (namespaced, e.g. `/sampo:intake`)

`intake` (parse §5, expand to briefs per §6, order the queue, apply AUTONOMY, start) · `next` / `resume` · `status` (≤ 25 lines) · `capture` · `tidy` · `doctor` · `reflect` (retro on last task/phase; writes learnings and decisions) · `improve` (§4.9).
Give skills specific, slightly "pushy" descriptions so they trigger reliably; keep each SKILL.md under ~500 lines with detail in `references/`. `rules/foreman.md` (always loaded, ≤ ~80 lines) contains only: the loop, the intake cheat sheet, the canonical order, the focus-lock table in compressed form, the subagent rule, the evidence rule, "state goes through fm," and pointers to the skills and `MASTER.md`.

### 4.6 Installed plugins on this machine: default decisions

These are defaults, not orders. Confirm them against recon in Phase 1 and get approval for every disable. Always-on figures were measured with `claude plugin details` in September 2026; re-measure, since versions move.

| Plugin | Default | Foreman role / reason |
|---|---|---|
| `ecc@ecc` | **disable** | ~29,100 always-on tokens (386 skills, 68 agents) plus 24 hook handlers, including synchronous PreToolUse hooks on every tool call, 7 Stop hooks, and SessionStart context injection. It is a complete competing system (memory persistence, continuous learning, planning). It is MIT-licensed: port the few skills or agents the user actually relies on (ask which) into Foreman or `~/.claude/skills/`, with attribution. If the user wants to keep it, the fallback is its env controls (`ECC_HOOK_PROFILE`, `ECC_DISABLED_HOOKS`, `ECC_SESSION_START_CONTEXT=off`); verify names in the installed version. |
| `superpowers@claude-plugins-official` | **disable** | A second orchestrator (brainstorm → plan → execute, plus a SessionStart hook). Before disabling, port three procedures into Foreman skill references, with MIT attribution: systematic debugging, test-first regression tests for FIX, and verification-before-completion. |
| `ponytail@ponytail` | keep | Complementary. Ponytail owns *how little code*; Foreman owns *what, when, and how it's verified*. R3 implied requirements stay within what ponytail also protects (validation, data-loss handling, security, accessibility, explicit requests) plus the tests and evidence §2.5 requires. Use its audit/debt skills as CLEAN-phase tools (verify skill names). Its hooks inject a ruleset at SessionStart, per prompt, and into subagents; count them in the load budget. |
| `claude-hud@claude-hud` | keep | Owns `statusLine`; Foreman adds none (§4.3). |
| `security-guidance@claude-plugins-official` | keep | SECURITY phase plus continuous checks. It registers SessionStart, UserPromptSubmit, PostToolUse, Stop, and SubagentStop hooks: make Foreman's Stop gate coexist with its Stop review (no double-blocking, no loops). |
| `code-review@claude-plugins-official` | keep | L-tier final review only (read-only agents, §8). |
| `context7@claude-plugins-official` | keep | R2 grounding for library/API questions. |
| `skill-creator@claude-plugins-official` | keep | Author and evaluate Foreman's skills. |
| `claude-md-management@claude-plugins-official` | keep | Decide one owner for CLAUDE.md lint (§10). |
| `plugin-dev@claude-plugins-official` | build only | ~1,700 always-on tokens; recommend disabling after Phase 8. |
| `pyright-lsp`, `typescript-lsp` (+ `clangd-lsp` etc. if servers exist) | keep | Diagnostics for grounding and verification. An LSP plugin whose server binary is missing just errors: disable it or install the server. |
| `playwright`, `chrome-devtools-mcp` | move to project/local scope | VERIFY for web UIs; chrome-devtools also for PERFORMANCE traces. chrome-devtools-mcp alone is ~520 always-on tokens. |
| `frontend-design` | keep | FEATURE work on UIs; tiny footprint. |
| `design@synced`, `figma@synced` | off unless doing design work | Synced from claude.ai: can be turned off locally, not uninstalled. |
| Trail of Bits pack (if installed with `--security`) | keep | SECURITY phase: `differential-review` for every security-relevant diff, `insecure-defaults`, `sharp-edges`, `static-analysis`, `supply-chain-risk-auditor`, `c-review` for C/C++. |
| `document-skills` (if installed with `--docs`) | keep | Only when a task produces Office/PDF files. |

Any plugin installed later gets the same treatment: map it to a lifecycle stage, scope it to the projects that need it, or flag it as a conflict.

### 4.7 Permissions: bypass by default

- `install.sh` sets `permissions.defaultMode: "bypassPermissions"` in `~/.claude/settings.json` (backup in `backups/`; `--no-bypass` skips it). That applies to every Claude Code session for this user, not only Foreman work. Claude Code shows its one-time warning on the next launch; the user accepts it themselves. Never set `skipDangerousModePermissionPrompt` on their behalf.
- Tool permissions and AUTONOMY are different things. Bypass removes tool-call prompts; AUTONOMY (§6.6) still decides which *plans* need approval, and BUILD_GATE still stops once after Phase 1.
- In bypass mode only deny rules, ask rules, and blocking PreToolUse hooks still stop a tool call. So:
  - **The guard hook is mandatory**, fails closed, and is tested in bypass mode (§12 scenario 13). Unless the active brief explicitly authorizes it, it blocks: recursive deletes of home, root, or anything outside the project and scratch dirs; force-push, `reset --hard`, or branch deletion on default branches; writes to credential material (`.env*`, `~/.ssh`, cloud/kube credentials, token files); piping downloaded scripts into a shell; disk, partition, firewall, and system-service changes (`mkfs`, `dd of=/dev/*`, `iptables`/`nft`, `systemctl` on system units); and publish/deploy/release commands. Every block message says how to authorize (add it to the brief's scope, then retry).
  - **Deny rules** for the truly catastrophic cases: propose a short list in `local/PLAN.md`, verify the rule syntax against the current permissions docs, test each one, and apply with approval.
  - **Per-repo opt-out:** `.claude/settings.local.json` with `"permissions": {"defaultMode": "auto"}` for repos with production credentials or client data. Foreman marks those projects `sensitive` and shows that at SessionStart.
  - **Recovery first:** commit or stash before any destructive-but-authorized step, so git and Claude Code's rewind both cover it.
- Expect these quirks and verify them on the installed version: writes under `~/.claude/` can still prompt in bypass mode, and subagents have not always inherited it. During the build, batch edits to `~/.claude/` so the user approves them once.

### 4.8 Visibility layer

The Claude Code binary is closed-source and replaced on every update, so never patch it. Everything here uses supported surfaces, is display-only (zero added model context unless stated), and keeps each render path under ~100 ms.

1. **Renderer.** Recommend fullscreen rendering (`/tui fullscreen`): mouse support, click-to-expand tool output, the live `/diff` side panel, `/focus`, and transcript search with `Ctrl+O`. Document it in MASTER.md; don't force it.
2. **Composed statusline.** Keep claude-hud. With approval, point `statusLine` at a Foreman wrapper (saving the original value for uninstall) that reads stdin once, runs the original claude-hud command with the same JSON, then prints one Foreman line from `fm state --line` (task, type, step n/m, queue, inbox, guard status, a bypass indicator). The wrapper also writes that JSON snapshot atomically to `state/sessions/<session_id>.json` for the dashboard (context %, cost, rate limits, cache hit ratio).
3. **Subagent rows.** Ship a default `subagentStatusLine` in the plugin's settings: each recon agent row shows task ID, scope, tokens vs. its context window, and elapsed time.
4. **Per-reply badge.** A `MessageDisplay` hook prefixes the first batch of each assistant message with `[T-0012 FIX · 3/5 · 14:02]`. It changes only what's on screen (the transcript and the model's view keep the original) and costs zero tokens. It may also mask secrets on screen. Fail open: on any error, the original text shows.
5. **Terminal integration** through `terminalSequence` (never `/dev/tty`): window/tab title (`foreman · T-0012 FIX 3/5`), taskbar progress (OSC 9;4) where supported, and desktop notifications when Foreman is blocked, needs an answer, or empties the queue.
6. **Clickable IDs.** Use `footerLinksRegexes` so task IDs like `T-0012` become footer badges that open the brief (verify the setting's format).
7. **Live dashboard: `fm watch`.** A stdlib-only curses TUI for a tmux split or second terminal showing: the active task with its step checklist and evidence; the queue in canonical order; the inbox; a live tool timeline (tool, target, duration, ok/fail) fed by async PostToolUse/PostToolUseFailure/SubagentStart/SubagentStop events; running subagents; files touched; guard blocks; hook latency percentiles; and the session snapshot from item 2. `fm watch --once` prints the same as plain text. `fm tidy` rotates the event file.
8. **History in Grafana.** The user already runs Grafana with InfluxDB and telegraf. Enable Claude Code's OpenTelemetry export into it (telegraf's OpenTelemetry input, or its Prometheus input scraping Claude Code's Prometheus exporter): cost and tokens by type, model, skill, plugin, and agent; tool results and durations; lines changed; commits; permission decisions. Keep prompt, response, and tool-content logging off (the defaults). Put the telegraf snippet and a dashboard JSON in `plugin/observability/`, and ask before touching their telegraf config.
9. **Theme (optional).** Ship one Foreman color theme in the plugin; the user picks it with `/theme`.
10. **Beyond the terminal (document only).** Agent view (`claude agents`) for many sessions at once, the Desktop app's Code tab for visual diffs and panes, and the Agent SDK if the user ever wants a fully custom UI.

### 4.9 Self-improvement (bounded and eval-gated)

Foreman improves its own skills, rules, hooks, and scripts over time. It does not improve the model, and it never changes itself unreviewed: an agent that rewrites its own instructions without a referee drifts and bloats, and one running in bypass mode that can edit its own guard has no guard.

- **Where ideas come from.** `reflect` after every task (what caused back-and-forth, what the user corrected, what the guard blocked that the user then allowed), repeated corrections in auto memory, doctor and tidy findings, hook latency and context-footprint trends, and recurring work (the same kind of task three or more times → propose a skill, built and evaluated with skill-creator). Each idea lands in Foreman's own INBOX as `source: self`, with its evidence. Nothing is applied directly.
- **Cadence.** `/sampo:improve` (on request, or suggested weekly or every ~10 completed tasks) turns the self-inbox into a proposal of at most five changes, each with evidence and an expected effect.
- **Isolation.** The live plugin loads in place from `FOREMAN_HOME/plugin` on `main`, so improvements are built in a git worktree on branch `improve/<date>` and tested with `--plugin-dir`, never in the live tree.
- **Referee.** `plugin/evals/` holds `claude plugin eval` cases: the §12 scenarios plus sanitized replays of real past tasks. Run the suite against the live plugin and the candidate with a cost ceiling (`--max-cost-usd`). A candidate qualifies only if it scores at least as well in every category, regresses no scenario, stays within the §2.8 budgets, and doctor is green.
- **Merging is the user's call.** Whatever AUTONOMY says, the user approves each self-improvement merge. Foreman then bumps the plugin version, updates MASTER.md and `CHANGELOG.md` in the same commit, and tags it (`claude plugin tag`). Rollback is `git revert` plus `/reload-plugins`.
- **Protected core.** Foreman may propose, but never apply, changes to the guard hook and deny rules, permission settings, the evidence and approval rules, the eval suite and its graders, and `BUILD_PROMPT.md`. The guard hook blocks agent writes to those paths; the user applies approved core changes or explicitly authorizes that single brief. This is a speed bump, not a sandbox: the real boundary is that nothing reaches `main` without the user.
- **Scope.** Improvements target Foreman itself. Lessons about a user's project go to that project's memory or CLAUDE.md through the normal flow.

---

## 5. The intake language

```
FIX: login times out after 30s on slow networks
FEATURE: export report as CSV @src/reports
CLEAN: collapse the three date helpers into one
PERF: dashboard first paint takes 4s
SECURITY: review the upload endpoint
CONTEXT: Django app; don't touch migrations
DONE-WHEN: all tests pass and the CSV opens in Excel
```

| Tag (case-insensitive) | Aliases | Meaning |
|---|---|---|
| CLEAN | REFACTOR, TIDY | behavior-preserving improvement |
| PERFORMANCE | PERF | measurable speed/resource improvement |
| SECURITY | SEC | vulnerability, hardening, audit |
| FIX | BUG | incorrect behavior → correct behavior |
| FEATURE | FEAT, CAPABILITY, CAP, ADD | new behavior |
| RESEARCH | SPIKE, INVESTIGATE | findings and a recommendation, no product code |
| CONTEXT | NOTE | background for every item in the block |
| CONSTRAINT | MUST, NEVER | hard rules for the block |
| DONE-WHEN | ACCEPT | block-level acceptance criteria |
| SKIP | OUT | explicitly out of scope |

**Modifiers:** `TAG!:` urgent (preempts order and current task, §7) · `TAG?:` exploratory (brief + options + recommendation; don't implement until confirmed) · `@path` scope hint · `#T-0012` relates to / depends on a task · indented lines continue the previous item · `NOW:` prefix = handle immediately.

**Untagged requests:** classify them yourself and state it in one line ("Treating this as FEATURE, tier M.").

**Canonical order:** `BASELINE → CLEAN → PERFORMANCE → SECURITY → FIX → FEATURE → FINAL VERIFY → REFLECT`
- **BASELINE** (automatic): confirm build and tests run, record baseline metrics for PERF items, snapshot git state. A FIX that blocks the baseline (broken build, tests can't run) is hoisted here, because refactoring on a broken base is unsafe.
- **CLEAN** first so later work lands in simpler code. Scope it to code the listed items will touch plus explicit CLEAN items; no drive-by rewrites. Tests must pass before and after; if coverage can't prove behavior is preserved, add characterization tests first.
- **PERFORMANCE**: measure before and after with the same method. No measurement, no performance claim.
- **SECURITY**: runs third, but any critical finding (exposed secret, auth bypass, RCE, reachable injection) is hoisted to run immediately, whatever phase you are in.
- **FIX** after structure settles, so fixes aren't undone by refactors. Each fix gets a regression test that fails before and passes after.
- **FEATURE** last, on a clean, measured, secure, correct base.
- Dependencies override order inside these rules (topological sort; report cycles). Re-plan between phases (§6.4).

---

## 6. Planning and self-reprompting

### 6.1 Passes (per item)
- **R1 Expand:** interpretation, assumptions with confidence, testable acceptance criteria, scope paths, non-goals.
- **R2 Ground:** read the real code, run the real commands, and correct R1 against reality. For M/L tiers, compare at least two approaches, choose one, and say why.
- **R3 Anticipate:** pre-mortem. Ask: "If I ship this and the user reviews it, what are the three most likely follow-ups or complaints?" Each one is either an **implied requirement** the user would obviously expect (error handling, edge cases, tests, updated docs/config, migration notes), which goes in scope, or an **adjacent idea**, which is captured to INBOX as a suggestion and not built. This is how you remove back-and-forth without gold-plating.
- **R4 Compile:** write the brief's **Execution Prompt**, a self-contained instruction block that a fresh session with zero conversation history could execute correctly (goal, context, files, steps, acceptance checks, verification commands, constraints). Then execute against your own Execution Prompt. This is the self-reprompting step, and it is what makes resume-after-compaction reliable.

### 6.2 Tiers

| Tier | Signal | Depth |
|---|---|---|
| S | ~≤30 lines, 1–2 files, obvious approach, low risk | brief ≤ 10 lines: R1 + light R2 + R4; still logged and verified |
| M | several files or a real design choice | full R1–R4, two approaches |
| L | cross-cutting, schema/API/security-sensitive, large, or uncertain | R1–R4 + adversarial self-critique + staged sub-tasks, each independently verifiable; optional read-only reviewer |

When unsure, go one tier up. Tiers may change after R2; say so when they do.

### 6.3 Brief template (`tasks/T-NNNN-<kebab>.md`)

```markdown
---
id: T-0012
type: FIX              # CLEAN|PERFORMANCE|SECURITY|FIX|FEATURE|RESEARCH
tier: M
status: planned        # captured|planned|active|verifying|done|blocked|deferred|dropped
priority: normal       # normal|urgent
scope: [src/auth/**, tests/auth/**]
depends_on: []
source: user           # user|discovered|followup
created: <iso>
updated: <iso>
---
# <imperative title>
## Raw request
> verbatim original text
## Interpretation
## Assumptions (confidence)
## Acceptance criteria
- [ ] <testable criterion> — verify with `<command>`
## Non-goals
## Approach (options → choice → why)
## Risks and rollback
## Execution prompt
<self-contained; regenerated at every re-plan checkpoint>
## Steps
1. ...  <- CURRENT
## Resume here
<what's done, what's next, uncommitted state>
## Verification evidence
- `<command>` → <result> (<timestamp>)
## Log
- <timestamp> <steering, scope change, decision>
## Follow-ups captured
- INBOX / T-refs
```

### 6.4 Re-plan checkpoints
After each phase group, and after any task that changed structure: re-read the remaining briefs, update scope paths, approaches, and Execution Prompts to match the new reality, re-order if dependencies changed, and log what changed. This is the "improve and re-prompt in order" step.

### 6.5 Plan rubric (self-critique before executing M/L and before any gate)
- Does every acceptance criterion have a verification command?
- Could a fresh session execute the Execution Prompt without asking anything?
- Is anything in scope that the user didn't ask for and wouldn't obviously expect? Move it to INBOX.
- Is anything the user would obviously expect missing? Add it.
- Is this the simplest adequate approach? What would make it fail?
- Is rollback clear?

Revise at most twice, then proceed or escalate.

### 6.6 Questions, autonomy, and loop caps
- **Batch questions** into one message, each with your default. Ask only what would make the work wrong or irreversible if guessed; otherwise proceed on the default and record the assumption.
- **AUTONOMY:** `guarded` = approve every plan. `standard` = approve plans for L tier, `?` items, and anything destructive or irreversible (data deletion, force-push, migrations on real data, major dependency bumps, public API changes); S/M tasks run after self-review. `autopilot` = only destructive/irreversible operations need approval.
- **Loop cap:** 3 failed verification attempts on the same step → stop, write a diagnosis (what was tried, what was observed, hypotheses), mark the task blocked, move to the next unblocked task, and report.

---

## 7. Focus lock (staying on track)

**One active task at a time. Finishing beats starting.** Classify every user message that arrives while a task is active:

| Class | Examples | Action |
|---|---|---|
| ANSWER | reply to a question you asked | apply it, log it, continue |
| STEER | "use the existing logger instead" (about the active task) | update the brief and log the change; if material, redo R2–R4; continue |
| NEW | new request, new intake lines, "also, can you..." | capture to INBOX verbatim with a type/tier guess, reply in one line ("Captured as T-0019 [FEATURE, M], queued after the current task. Say NOW to switch."), continue. **Do not implement.** |
| QUESTION | "how does X work?", chat | answer concisely without editing files or state, then continue |
| PREEMPT | `NOW:`, `TAG!:`, "prod is down" | checkpoint, switch, and offer to resume afterward |
| PAUSE | "stop", "pause", "hold on" | checkpoint and wait |

- Ambiguous between STEER and NEW → treat as NEW and say so; the user can reply "that's for the current task."
- **Discovered work** (bugs, smells, missing tests, ideas noticed along the way) → capture with `source: discovered`. Don't fix inline unless it blocks the active task's acceptance criteria; then record it as a dependency, make the minimal fix, and log it.
- **Scope guard:** an edit outside the active brief's scope produces a factual note; either widen scope with a logged justification (genuinely required) or capture it as a new task.
- **Checkpoint:** update the current-step marker and "Resume here," commit WIP to the task branch or create a named stash (`foreman/T-0012`), update STATE, log the event.
- **Resume** (after interruption, compaction, or a new session): read STATE and the active brief, state the resume point in one line, continue.
- **When idle:** a plain request is itself intake. Classify, expand, plan (tiered), then execute per AUTONOMY. Never start editing without at least an S-tier brief.
- **End of each task:** triage INBOX (expand captured items into briefs, fold them into the queue in canonical order), show the updated queue in ≤ 10 lines, continue per AUTONOMY.

---

## 8. Subagents

Default: **none.** The main thread plans, edits, and writes all state.
Allowed only for: (a) read-only recon of large codebases, (b) web/doc research, (c) dependency or security audits, (d) optional independent read-only review of L-tier changes.

Contract:
- A self-contained brief (question, scope paths, what to ignore, output format), never the conversation history.
- Read-only tools only (read, search, web fetch; read-only shell if essential). No edit/write tools.
- Output ≤ 400 words, every claim backed by `path:line` or a URL, confidence per finding, and an explicit "not checked" list.
- The main thread saves the summary into `research/` and spot-checks at least two claims before relying on it.
- Run in parallel only with disjoint scopes, at most 3 at once.
- Installed plugins whose agents implement code are not used for implementation; their review agents may be used for (d).

Define `fm-recon` (and optionally `fm-reviewer`) as plugin agents with restricted tool lists.

---

## 9. Memory and hygiene (`fm tidy`)

Dry-run by default; `--apply` to act; archive before deleting; log every change.
- Regenerate `STATE.md` from the briefs.
- Done/dropped tasks older than 14 days → `archive/YYYY-MM/`. Roll large ledgers into monthly summaries.
- INBOX items untouched for 30 days → list for a keep/drop decision. Never auto-delete user requests.
- **Auto memory:** keep `MEMORY.md` within its startup window (~200 lines); dedupe; flag entries that reference paths or commands that no longer exist; resolve contradictions in favor of the newest verified entry; move detail into topic files.
- **CLAUDE.md and rules lint:** size budgets, dead paths, stale commands (check against Makefile, package.json, etc.), rules duplicated between CLAUDE.md and `rules/`, contradictions.
- **Knowledge graph** (only if a graph/memory MCP is installed): merge duplicate entities, remove orphan relations, prune observations tied to archived tasks; show the diff first.
- **Task graph:** dependency cycles, dangling `depends_on`, blocked tasks whose blocker is done.
- **Plugin footprint:** report always-on token cost per plugin (`claude plugin details`) and context injected by other plugins' hooks; flag plugins not used recently, LSP plugins whose server binary is missing, and newly installed plugins that §4.6 hasn't classified. Synced plugins can be turned off locally, not uninstalled.
- **Cadence:** a cheap "tidy overdue" check at SessionStart; full tidy after each completed queue or via `/sampo:tidy`; never at SessionEnd.

---

## 10. CLAUDE.md format

**Principles**
- CLAUDE.md loads in full every session, so every line costs context on every turn. It holds *instructions*, not history or status.
- Test each line: "Would removing this cause a mistake?" If not, delete it.
- No task status (that's STATE), no learnings log (that's auto memory), no long docs. Reference long docs by plain path (read on demand); an `@import` loads every session, so reserve it for things needed every session.
- Path-specific guidance goes in `.claude/rules/<topic>.md` with `paths:` frontmatter.
- Foreman-managed content lives between `<!-- foreman:begin -->` / `<!-- foreman:end -->` markers so `fm` can update it without touching human-written lines.
- Never rewrite the user's existing CLAUDE.md content. Add only the marked block; propose any cleanup in `PLAN.md` instead of applying it.
- One owner for CLAUDE.md upkeep: if `claude-md-management` is installed, decide in `PLAN.md` whether it or `fm tidy` owns the lint.

**Global `~/.claude/CLAUDE.md`** (≤ 50 lines)

```markdown
# Me
- <stable preferences: language/spelling, tone, preferred tools>
# Environment
- <OS, shell, package managers, where repos live>
<!-- foreman:begin -->
# Foreman
Foreman is installed. Operating rules: ~/.claude/rules/foreman.md. System map: ~/.claude/foreman/MASTER.md.
<!-- foreman:end -->
```

**Project `CLAUDE.md`** (≤ 150 lines)

```markdown
# <Project> — <one-line purpose>

## Commands
- Build: `...`
- Test (all / single): `...` / `...`
- Lint/format: `...`
- Run: `...`

## Map
- `src/api/` — <responsibility>   (≤ 15 lines, non-obvious only)

## Conventions
- <only rules Claude would otherwise get wrong>

## Definition of done (this repo)
- <commands that must pass; review and doc expectations>

## Hazards
- <generated files, never-touch paths, secrets handling, prod-adjacent commands>

<!-- foreman:begin -->
## Foreman
- Project slug: <slug> · State: ~/.claude/foreman/state/projects/<slug>/
- Canonical verification: `<command>`
<!-- foreman:end -->
```

When Foreman registers a project that has no CLAUDE.md, draft one from recon and show it before writing (subject to AUTONOMY).

---

## 11. MASTER.md

Create `FOREMAN_HOME/MASTER.md`: the single document that explains the whole system to the user and to you. It is not loaded every session (`rules/foreman.md` points to it); read it whenever you are unsure where something lives or how parts interact. Required sections:

1. **What Foreman is** (one paragraph) and the loop.
2. **How to prompt it:** examples from a one-liner to a multi-item block; override words (`NOW:`, `PAUSE`, `RESUME`, `STATUS`, "that's for the current task"); slash commands. Make it clear that a plain one-line request gets the full treatment.
3. **File map:** every file and directory Foreman created or depends on, with path, purpose, owner (user / Claude / fm / Claude Code), when it is loaded, and size budget. Includes `BUILD_PROMPT.md`, `PLAN.md`, `recon.md`, `rules/foreman.md`, the CLAUDE.md files, skills, agents, hooks, state files, auto memory, and backups.
4. **Interaction diagram** (Mermaid): prompt → UserPromptSubmit → rules/skills → fm → state → SessionStart/PreCompact → resume, plus the Stop and TaskCompleted gates.
5. **Lifecycle walkthrough:** one example from a `FIX:` line to an archived task, naming every file touched.
6. **Source-of-truth table** (§4.2) and **precedence:** the user's current message > project CLAUDE.md and rules > `rules/foreman.md` > skill defaults. Safety guards are never overridden.
7. **Integrations:** each installed plugin/MCP → lifecycle stage → how it is invoked; disabled or conflicting ones and why.
8. **Relationship to BUILD_PROMPT.md and the repo:** BUILD_PROMPT.md is the origin spec; the repo is how Foreman travels between machines (`install.sh` one-liner). Changes to Foreman itself go through intake as tasks against `FOREMAN_HOME/plugin`, and MASTER.md is updated in the same commit (doctor fails if the file map drifts from disk).
9. **Operations:** the dev loop (edit `plugin/` → `/reload-plugins`), disable (`claude plugin disable sampo@sampo`), uninstall, restore a backup, logs, running doctor and tidy, and updating another machine (`git pull` or rerun `install.sh`).
10. **Known limitations.**

---

## 12. Verification

**`fm doctor` checks:** settings JSON valid; every hook script exists and is executable; each hook runs against fixture payloads within budget; no two hooks write the same file; no skill/command name collisions; always-on footprint within budget; skill and agent frontmatter valid; every brief parses and STATE matches the briefs; MASTER.md file map matches disk; backup present; plugin loads with no errors; `claude plugin validate --strict` passes for the marketplace and plugin; nothing gitignored is tracked and no secrets are staged; `statusLine` is unchanged from recon; `install.sh --no-plugins` and `setup-plugins.sh --dry-run` still run cleanly.

**End-to-end scenarios** (in a scratch git repo containing a tiny app with tests; drive them with `claude -p` plus `--plugin-dir` where feasible, otherwise simulate with fixture payloads and say which ones were simulated):
1. A mixed 5-item intake block → briefs created, correct canonical order, dependencies respected.
2. A new request mid-task → captured, not implemented, original task resumed.
3. A steering message → brief updated, work continues.
4. `NOW:` preemption → checkpoint, switch, later resume at the exact step.
5. `/compact` mid-task → state re-injected, correct resume.
6. Premature finish with a failing test → gate blocks once, no loop.
7. Out-of-scope edit → scope note appears and is handled per policy.
8. Recon subagent → read-only, bounded output, no writes.
9. Seeded rot (dead path in CLAUDE.md, duplicate memory lines, 40-day-old inbox item, dependency cycle) → tidy dry-run finds all of it; apply archives, nothing lost.
10. Corrupted state file → non-guard hooks fail open and log; guards still work.
11. Plugin disabled → Claude Code works normally with zero Foreman errors.
12. Uninstall → reinstall → identical behavior; uninstall leaves nothing behind except `state/` (and asks about that), and the original `statusLine` and permission mode are restored.
13. In bypass mode, each guard category from §4.7 is blocked with an actionable message, the same command succeeds once the brief authorizes it, and a sensitive repo's opt-out takes effect.
14. Visibility: the statusline shows claude-hud's lines plus the Foreman line; the MessageDisplay badge appears while the transcript stays unchanged; `fm watch` shows a new tool call within 2 seconds; telemetry reaches the collector (or the snippet is ready and approval is pending).
15. Self-improvement: a seeded `source: self` inbox item produces a proposal, a candidate built in a worktree, and an eval comparison against the live plugin, then stops for approval; an agent write to the guard hook or eval suite is blocked.

---

## 13. Final report (≤ 40 lines)

What was built; deviations from this spec and why; integration and conflict decisions; always-on context cost before vs. after (CLAUDE.md + rules + plugins + hook injections, from the Phase 0 baseline); hook latency numbers; scenario results; known limitations; and the 5-line "how to use it" section from MASTER.md.

<br>

---
---

## APPENDIX A — Plugin decisions (rationale; the executable version is `setup-plugins.sh`)

> Read this to understand the choices. Don't duplicate its commands elsewhere: `setup-plugins.sh` is the single source for install logic (flags: `--build-tools`, `--security`, `--docs`, `--apply-conflicts`, `--dry-run`). All plugin IDs below were verified against the live marketplaces, and token figures were measured with `claude plugin details`, in September 2026.

### A.1 Core (installed if missing)
`context7`, `security-guidance`, `code-review`, `skill-creator`, `claude-md-management` from `claude-plugins-official`, plus `plugin-dev` while building (`--build-tools`). Combined always-on cost: ~200 tokens, plus ~1,700 for plugin-dev during the build.

### A.2 Language servers
Installed only when the server binary is already on PATH: `clangd-lsp` (clangd), `pyright-lsp` (pyright-langserver), `typescript-lsp` (typescript-language-server), `gopls-lsp` (gopls), `rust-analyzer-lsp` (rust-analyzer). No always-on token cost; an installed LSP plugin without its server only produces errors.

### A.3 Optional packs
- **Security** (`--security`, marketplace `trailofbits/skills`): `differential-review`, `insecure-defaults`, `static-analysis`, `sharp-edges`, `supply-chain-risk-auditor`, `c-review`. About 1,200 always-on tokens in total. Worth it for security-heavy work; they plug straight into the SECURITY phase.
- **Documents** (`--docs`, marketplace `anthropics/skills`): `document-skills` (docx/xlsx/pptx/pdf), ~760 tokens. Only if Claude Code should produce Office/PDF files.
- **Per project only:** `claude-api@anthropic-agent-skills` (~270 tokens) in repos that build on the Claude API or Agent SDK. Install with `--scope project`.

### A.4 Not recommended alongside Foreman

| Plugin | Why |
|---|---|
| `ecc` | ~29,100 always-on tokens and 24 hook handlers; a full competing memory/learning/planning system. See §4.6. |
| `superpowers` | Competing orchestrator. Foreman ports its three best procedures (§4.6). |
| `feature-dev` | Duplicates intake and planning with parallel agents. |
| `ralph-loop` | Stop-hook loop that collides with Foreman's completion gate. |
| `example-skills` (anthropics/skills) | ~940 tokens for 12 mostly unrelated skills; duplicates `skill-creator` and `frontend-design`. Cherry-pick a single skill folder into `~/.claude/skills/` if one is useful (e.g. `mcp-builder`). |
| `ask-questions-if-underspecified` (trailofbits) | Conflicts with Foreman's batched-questions-with-defaults policy (§6.6). |
| Extra memory plugins, output-compression plugins | A second memory store drifts; terse output degrades briefs and handoffs. |

### A.5 Finding more
Browse `claude.com/plugins` (install counts; `/plugin` shows per-plugin context cost). Trail of Bits publishes a vetted list of marketplaces at `trailofbits/skills-curated`, which is a better filter than popularity. Before adding anything, check `claude plugin details` and its hooks; then classify it per §4.6.
