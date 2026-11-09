<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="design/sampo/logo/readme-header-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="design/sampo/logo/readme-header-light.svg">
  <img src="design/sampo/logo/readme-header-dark.svg" width="720" alt="Sampo: a steel anvil on a rune-cut stone bound with a riveted band, a many-coloured fire behind it, beside the Sampo wordmark">
</picture></p>

<h1 align="center">Sampo</h1>

<p align="center"><i>formerly Foreman</i></p>

<p align="center"><b>Sampo does the work on its own.</b><br>
It finds what your repo needs, plans it, builds it in parallel lanes, proves it,<br>
lands it through a merge train and ships it, without you prompting, waiting or checking.<br>
The discipline layer for coding agents: Claude Code, Codex, Gemini CLI, opencode, or its own <code>sampo smith</code> on any model.</p>

<p align="center"><a href="https://github.com/ChaseSunstrom/sampo/actions/workflows/test.yml"><img src="https://github.com/ChaseSunstrom/sampo/actions/workflows/test.yml/badge.svg" alt="tests"></a></p>

<p align="center">
  <a href="#quick-start"><b>Install</b></a> ·
  <a href="#how-it-works"><b>How it works</b></a> ·
  <a href="#kalevala-it-improves-itself"><b>Kalevala</b></a> ·
  <a href="#the-ward"><b>The Ward</b></a> ·
  <a href="#features"><b>Features</b></a> ·
  <a href="MASTER.md"><b>Docs</b></a> ·
  <a href="https://chasesunstrom.github.io/sampo/"><b>Site</b></a>
</p>

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screens/board-dark.png">
  <img src="docs/assets/screens/board-light.png" width="960" alt="The live Board, nine numbered panels: four builder lanes and the files they hold, a merge train running at the gate, CPU by kind of work against a 10% goal, the task in focus from the words asked to its brief and steps, the card and moon library, which models each job calls, the queue and its dependency graph, Sampo's state folder and latest decision, and a cell per test module">
</picture></p>
<p align="center"><sub>The live Board (<code>sampo watch --web</code>) on Sampo's own project, fed here the snapshot the test suite keeps (<code>plugin/tests/fixtures/live/snap-5.json</code>) so no private state shows.</sub></p>

## What it does

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/what-it-does-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/what-it-does-light.svg">
  <img src="demo/figures/what-it-does-dark.svg" width="880" alt="What it does: a table of 9 rows (Sampo, So that); its text follows">
</picture></p>
<details><summary>As text</summary>

| Sampo | So that |
|---|---|
| **Finds the work** | `sampo find` ranks bugs, untested code, slow paths and stale docs with no model; `sampo scout` files work nobody asked for. Plain words work too. |
| **Plans before it edits** | Every request becomes a brief: type, size, what done means and the command that proves it. No brief, no edit. |
| **Builds in parallel** | Each task gets a lane: its own git worktree and a builder agent that writes the failing test first. `sampo lane max` sets how many run at once. |
| **Lands in one gate run** | The merge train takes up to 8 finished lanes, runs the tests they reach once, names a red run's culprit and lands the rest. |
| **Proves it** | Evidence is a command Sampo ran and recorded, never a sentence. Nothing closes without its audits. |
| **Ships it** | Green work fast-forwards main; `sampo ship` pushes it and `sampo ci-watch` turns a red CI run into a fix task. |
| **Improves itself** | Kalevala runs a Rune tower per aspect that rewrites its own process each generation, kept only when it scores better on the exam. |
| **Guards bypass mode** | A fail-closed Ward stops wide deletes, force-pushes, credential reads and `curl \| sh`, and snapshots work before git throws it away. |
| **Stays cheap** | A rule before a local model, a local model before a cloud one; only the tests a change reaches; hooks under their time budget; `sampo budget` caps spend. |

</details>

## How it works

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/diagrams/loop-dark.svg">
  <img src="docs/assets/diagrams/loop-light.svg" width="960" alt="The loop in five numbered panels: your words, sampo find and sampo scout become a T-NNNN brief; the brief fans out to three lanes, each a builder in its own worktree; the lanes feed one merge train; green work goes to task finish and sampo ship; lessons from the ship feed the next round of finding">
</picture></p>

Every request, however terse, goes round the same loop, and so does work nobody asked for:

- **Brief.** Intake turns words or findings into a brief: its type, its size (S, M or L), what done means with the command that proves it, and the steps.
- **Build.** `sampo lane brief` hands each task to a builder in its own worktree; edits wait for the brief, and every command passes the Ward.
- **Land.** `sampo lane land` merges finished lanes through the train below; each landed task closes through `sampo task finish`'s checks.
- **Learn.** Lessons, fixes and decisions are recorded, and the next queued task starts by itself.

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/diagrams/merge-train-dark.svg">
  <img src="docs/assets/diagrams/merge-train-light.svg" width="860" alt="Lanes and the merge train: four lanes, each a worktree on its own foreman/T-NNNN branch, merge in order in a scratch integration worktree and run the gates once; red names the culprit from the gate's own output and parks it while the rest land; green fast-forwards main, closes each task and runs the full suite in the background">
</picture></p>

Builders never touch each other's files, and finished lanes land together: the train merges them in a scratch worktree (never main), runs only the tests the batch reaches, and a red run parks its culprit, not the batch.

## Kalevala: it improves itself

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/diagrams/kalevala-dark.svg">
  <img src="docs/assets/diagrams/kalevala-light.svg" width="880" alt="Kalevala: towers for initiative, efficiency, self-repair and 23 more aspects, each from a sealed gen-0, and the shared moons, lessons and steers all feed one generation: a seed of up to eight missions, built as tasks in lanes, critiqued and scored on Louhi's seeded exam, selected only if it beats its parent, then reviewed and swept">
</picture></p>

Sampo is built by Sampo, and so is the way it builds. Kalevala runs one Rune tower per aspect (26 here), each climbing generation after generation unattended and rewriting its own seed, missions and lenses.

A rewrite stays only if it closes more exam scenarios than its parent, and what one tower learns, every tower reads. `sampo kalevala status` lists the towers, `sampo kalevala step` runs the next round, `sampo rune step` names one tower's next action.

<details><summary>The names</summary>

**The names.** They come from the Kalevala. Ilmarinen the smith forged the Sampo, the mill that makes by itself: that is **Smith**, the task loop that plans, builds and proves. Väinämöinen sang runes that made things: **Rune**, the tower that writes itself toward a goal. Louhi set impossible tasks: **Louhi**, the exam. The stolen sun and moon were forged anew: **Moons**, the ideas worth forging. Lost words were fetched back from the underworld: **Underworld**, the not-yet ledger. And the **Ward** guards.

</details>

## By the numbers

Sampo builds itself, so its own history is the plainest measure: tasks closed a day, and the test suite those tasks grew. Drawn from git by `python3 demo/readme_figures.py --refresh`.

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/chart-closed-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/chart-closed-light.svg">
  <img src="demo/figures/chart-closed-dark.svg" width="410" alt="Tasks closed per day over the last 30 days, from git log">
</picture>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/chart-tests-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/chart-tests-light.svg">
  <img src="demo/figures/chart-tests-dark.svg" width="410" alt="Test functions in the suite over time, from git grep at each date">
</picture></p>

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/ChaseSunstrom/sampo/main/install.sh | bash
```

`fm` still works as an alias.

Or from inside Claude Code:

```
/plugin marketplace add ChaseSunstrom/sampo
/plugin install sampo@sampo
```

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/quick-start-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/quick-start-light.svg">
  <img src="demo/figures/quick-start-dark.svg" width="880" alt="Quick start: a table of 4 rows (Then, How); its text follows">
</picture></p>
<details><summary>As text</summary>

| Then | How |
|---|---|
| **Ask for something** | `fix the login timeout on slow wifi`, in plain words. That's the whole interface. |
| **Watch it first** | `sampo demo` runs the real `sampo` and Ward hook on a seeded bug in a throwaway repo: no model calls, nothing left behind. |
| **See where things stand** | Bare `sampo` in a project says what's next; `sampo help` answers "can it…?" in plain words; `sampo which NEED` searches past fixes, your tools, MCP servers, binaries, skills and plugins at once, and says which hits need a grant. |
| **Let it run** | Say `FULL AUTO`: it works the whole queue, then finds its own next work. `sampo watch --web` opens the Board; `sampo lane max` sets how many builders run at once. |

</details>

<details>
<summary><b>Templates and new tools</b></summary>

Starting a page, a 3D scene or a `sampo smith` panel? Start from a template that already passes the check that keeps it strict:

```bash runnable
sampo help can scaffold an mcp server   # the commands whose help answers it, or a plain no
sampo new list                                  # the templates: web, scene, panel
sampo new web site                              # site/index.html: tokens, light and dark, no outside hosts
sampo design lint site                          # still clean: no generic-design tells
```

Nothing found for a missing tool or skill? Build it: `sampo new tool NAME` writes `tools/NAME.py` and a `tests/test_NAME.py` that fails until it works; `sampo new skill NAME` writes a project skill in the listing's format.

</details>

<details>
<summary><b>More ways to install, requirements and flags</b></summary>

The first session puts `sampo` on your PATH (`~/.local/bin/fm`, made when nothing is there and repaired after an update; anyone else's `sampo` stays). `sampo doctor` fails with the line to add to your shell profile when a new terminal still can't find it. `eval "$(sampo complete --shell bash)"` in your `~/.bashrc` (or `--shell zsh`) gives tab completion for commands, options and live task ids. `sampo demo --pace 1` plays the demo slowly enough to follow.

The plugin-system install changes nothing outside the plugin: the hooks and the Ward run, and the first session reads Sampo's rules from the plugin. Say "finish setting up Sampo" later to add the rest (status line, deny rules, the bypass question, `sampo` in your shell). `sampo uninstall-user` undoes it.

**Having Claude install it for you?** Tell it: "install Sampo following https://github.com/ChaseSunstrom/sampo/blob/main/INSTALL.md". [INSTALL.md](INSTALL.md) has it explain the defaults and ask before each change.

**Private repo** (uses your `gh` login):

```bash
gh repo clone ChaseSunstrom/sampo ~/.claude/foreman && ~/.claude/foreman/install.sh
```

**Requirements:** Linux, macOS or WSL (not native Windows), `git`, and Python 3.12.7 or newer as `python3`. Claude Code (`claude`, tested on 2.1.304; the pane needs 2.1.299 or later) is optional: without it the installer skips the Claude Code plugin and settings, puts `sampo` on your PATH and wires Codex, Gemini CLI or opencode if it finds them (or points you at `sampo smith`). When Python is older or missing, the installer gets it with Homebrew on macOS or with `uv` elsewhere (no sudo; `python3` goes in `~/.local/bin`).

**Flags:**

- `--security`: Trail of Bits security skills.
- `--docs`: Office and PDF skills.
- `--apply-conflicts`: disable plugins that compete with Sampo instead of only reporting them.
- `--no-plugins`: skip the curated plugin setup.
- `--bypass` / `--no-bypass`: make bypass mode your default, or keep your current permission mode, without being asked.
- `--local-models` / `--no-local-models`: pull the pinned Ollama embedding models (bge-m3, nomic-embed-text) for the moons funnel, or don't and don't ask. Needs Ollama installed; the installer never installs it.
- `--no-mod`: skip the sampo-ui mod.
- `--no-wiring`: don't touch `~/.claude`.
- `--build`: open Claude Code on `/sampo:build` to rebuild from `BUILD_PROMPT.md`.
- `--desktop`: also install Sampo Desktop from its newest release (the AppImage and a menu entry on Linux x86_64, the app in `~/Applications` on macOS).

**What `install.sh` does:**

1. Clones this repo to `~/.claude/foreman`, or fast-forwards it if it's clean and on `main`.
2. Runs `setup-plugins.sh`. It installs the curated plugins that are missing, and installs language-server plugins only when the server is on your PATH. It reports conflicting or heavy plugins but never uninstalls anything.
3. Registers this repo as a local plugin marketplace and installs `sampo@sampo` (an install from before the marketplace was named `sampo` is moved by `sampo migrate plugins` instead). The plugin loads in place, so edits under `plugin/` apply on `/reload-plugins`.
4. Asks whether to make `bypassPermissions` your default permission mode. That is Sampo's recommended mode, because its Ward is built for it. On a yes (or `--bypass`) it sets it, backing up `settings.json` and recording the original. With no terminal to ask on and no `--bypass`, your permission mode stays as it was.
5. Wires Sampo into `~/.claude` with `sampo install-user` unless you pass `--no-wiring`. That adds:
   - a statusLine wrapper (your original line and claude-hud still render above the Sampo line);
   - deny rules for catastrophic commands;
   - a raised Stop-hook continuation cap for drive mode;
   - a marked block in `~/.claude/CLAUDE.md` and the `~/.claude/rules/sampo.md` symlink;
   - a `~/.local/bin/fm` link so `sampo` works in any shell (unless a different `sampo` is already there).

   Every change is recorded in `state/install-manifest.json`, so it can be undone exactly.

</details>

## Talking to it

Plain requests work. Tags and a few lines of context make them precise:

```
fix the login timeout on slow wifi            # plain words work: classified, planned, proved
FIX: login times out after 30s on slow networks
FEATURE: export report as CSV @src/reports
CLEAN: collapse the three date helpers into one
CONTEXT: Django app; don't touch migrations
DONE-WHEN: all tests pass and the CSV opens in Excel
```

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/talking-to-it-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/talking-to-it-light.svg">
  <img src="demo/figures/talking-to-it-dark.svg" width="880" alt="Talking to it: a table of 4 rows (You say, What happens); its text follows">
</picture></p>
<details><summary>As text</summary>

| You say | What happens |
|---|---|
| `NOW:` · `PAUSE` · `STATUS` | switch tasks on purpose · checkpoint · where things stand |
| "super improve it" | a brainstorm first: each idea checked against the real code, the best ones queued |
| `FULL AUTO` | the whole queue without questions; decisions recorded, one summary at the end (`STANDARD AUTONOMY` switches back) |
| "is sampo ok?" · "clean up" · "this repo is sensitive" | Claude runs `sampo doctor`, `sampo tidy` or `sampo sensitive on`. You never have to type a command. |

</details>

Work runs in the order CLEAN → PERFORMANCE → SECURITY → FIX → FEATURE, after a baseline check.

<details>
<summary><b>Brainstorms, audits, review workflows and docs</b></summary>

- **Open-ended requests** ("super improve it", "just get it done") get a brainstorm first. Several tool-less sub-agents each look through a different lens, then Claude checks every idea against the real code and queues the best. A brainstorm runs up to six short `claude -p` (Sonnet) sessions, which count against your plan's usage.
- **Audits before done:** finished work is checked through independent lenses: intent, adversary, edge cases, operations and maintainability. Small tasks get a self-check; large ones get all five. A change that touches UI also gets a design read: an art director, an accessibility reviewer and a first-time user, with the task's screenshots.
- **Review:** `/sampo:review [range | branch | T-0123]` runs one reviewer per lens over a diff, then two skeptics try to refute every finding, so only confirmed defects come back, with the `sampo task finding` lines to record them.
- **Ideas:** `/sampo:ideas "<request>"` brainstorms with agents that read the code while they think, so each idea arrives marked new, partial or already built, with evidence and a ready intake block. Both workflows are multi-agent runs and cost more than a single pass.
- **Docs stay current:** bigger tasks can't close until they record which docs they changed, and those docs must exist and match the code. Drift elsewhere is reported when a task closes (`sampo docs` shows it any time).
- Long sessions compact earlier, since Sampo's state makes that cheap.

</details>

## The Ward

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="site/assets/diagrams/guard-dark.svg">
  <img src="site/assets/diagrams/guard-light.svg" width="880" alt="The Ward: every command passes the Ward hook first; tests, builds and in-scope edits run; commands that would discard uncommitted work are snapshotted first; dangerous ones are refused with how to grant them">
</picture></p>

Sampo is built for bypass mode (no tool-call prompts), which the installer offers to turn on. What still stops a dangerous command is Sampo's Ward hook, plus 20 deny rules for the truly catastrophic cases.

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/the-ward-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/the-ward-light.svg">
  <img src="demo/figures/the-ward-dark.svg" width="880" alt="The Ward: a table of 6 rows (Area, Stopped until you grant it); its text follows">
</picture></p>
<details><summary>As text</summary>

| Area | Stopped until you grant it |
|---|---|
| **Deletes** | recursive deletes outside the project; recursive or world-writable `chmod`/`chown` on /, the home folder or system folders |
| **Git** | force-pushes and hard resets on default branches |
| **Secrets** | reading credential files (~/.ssh keys, ~/.aws/credentials, ~/.netrc …), sending the environment or a key over the network, `curl \| sh` |
| **System** | disk, firewall and system-service changes |
| **Outward** | publish and deploy commands; starting a remote session |
| **Sampo itself** | direct writes to Sampo state; edits to Sampo's protected core and to Claude Code's `~/.claude.json` |

</details>

A blocked command says how to authorize it for the current task. Anything that needs you (Sampo's own code, a remote session, installing plugins or MCP servers, destructive categories) comes as Claude Code's own permission prompt, saying exactly what a yes grants.

<details>
<summary><b>Snapshots, the risk budget and other protections</b></summary>

- The Ward reads every Bash command and file edit before it runs, including the commands sampo stores and runs later.
- Work snapshots save uncommitted work before `git reset --hard`, `checkout -- .`, `clean -f` and the like. `sampo snapshots` lists them, each with the command that brings it back; `sampo oops` shows the newest and `sampo oops --apply` restores it.
- `sampo sensitive on` keeps a repo's content from leaving the machine; `sampo sensitive suggest` lists what hints a repo needs it (`.env*`, `*.pem`, secret or prod in a path) and changes nothing.
- A daily risk budget: destructive Ward blocks, failed closes and reverts spend points, and past the cap Claude's autonomy steps down a notch until tomorrow (`sampo risk`; only you move the cap).
- Install commands are checked before they run: a package one edit from a popular name, or published in the last 30 days, gets a note (`sampo deps check "<cmd>"`). A command copied from a web page or MCP result asks before it runs.
- `sampo posture lite` (yours to type: `! sampo posture lite`; Claude may not lower it) keeps only the Ward and the evidence gate in a repo where the rest of Sampo is in the way (no injected notes, no drive); `sampo posture full` brings it back.

</details>

<details>
<summary><b>Standing yes, trust mode, sensitive repos and turning bypass off</b></summary>

**Working on Sampo itself a lot?** Say "don't ask to edit Sampo". One prompt (`sampo ask ID core --standing`) asks for a standing yes. It covers Sampo's own code, rules and evals for every later task, until `sampo standing off`. Claude Code's settings, Sampo state and the Ward file itself still ask each time.

**Trust mode.** To let Claude edit those too (the Ward and Claude Code's settings, never Sampo state), type `/fm-trust on` yourself. The sampo-ui mod only accepts it from your own prompt (or your phone), never from Claude, a scheduled prompt or a skill, and the Ward refuses any attempt by Claude to turn it on. The band shows `⚠ trust on` while it lasts. `/fm-trust off` (or `sampo trust off`) ends it. Other questions come as a prompt too.

**Risk budget off switch.** The daily risk budget steps Claude's autonomy down when guard blocks, failed closes and reverts pile up. To turn it off, type `/fm-risk off` (every project) or `/fm-risk off here` (this one) yourself; `/fm-risk on [here]` restores it. Like `/fm-trust`, only your own prompt is accepted and the Ward refuses `sampo risk off` from Claude. `sampo risk`, the band (`risk off`) and `sampo doctor` say who set it and when. A destructive block that the same session then passes with a rewrite of the same command costs no point.

**Fewer dialogs outside bypass mode.** In Claude Code's default mode every Bash command off your allow list asks. Type `! sampo approve-reads on` and Sampo answers that dialog for commands that only read (`ls`, `cat`, `grep`, `git status` or `git log`, `find` without `-exec` or `-delete`, …).
- Every part of the command has to be one of those without a writing flag, with no redirect except to `/dev/null` and no `$` (a variable can print a secret). Anything else still asks.
- Only you can turn it on (the Ward refuses it from Claude); `sampo approve-reads off` ends it.

**Sensitive repos.** To opt a repo out (client code, anything with production credentials), tell Claude "this repo is sensitive" (it runs `sampo sensitive on`). That writes this to the repo's `.claude/settings.local.json`, kept out of commits via `.git/info/exclude`, and Sampo shows the repo as sensitive at session start:

```json
{ "permissions": { "defaultMode": "default" } }
```

Use `"default"` (or `"acceptEdits"`), not `"auto"`: Claude Code ignores `auto` and `bypassPermissions` in project and local settings. `sampo sensitive off` undoes it.

**Turning bypass off.** To turn bypass off everywhere, set `defaultMode` back to `"auto"` (or delete it) in `~/.claude/settings.json`. The original is in `~/.claude/foreman/backups/`.

</details>

## Features

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/features-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/features-light.svg">
  <img src="demo/figures/features-dark.svg" width="880" alt="Features: a table of 14 rows (What you get, Try); its text follows">
</picture></p>
<details><summary>As text</summary>

| | What you get | Try |
|---|---|---|
| **Plans** | Briefs with type, tier, criteria and steps; M and L work lists approaches first; the queue orders itself | `sampo queue` |
| **Knows your stack** | Over 1,000 stack cards in 29 areas; a card's pitfalls arrive with the first edit of a file it covers | `sampo lib` |
| **Verifies** | Gates that reuse a pass on an identical tree; a fix needs a test that failed first | `sampo check` · `sampo task prove` |
| **Shows what happened** | A branch ranked by review risk; any line traced back to the words you asked in | `sampo tour` · `sampo why` |
| **Runs unattended** | Drive mode, full auto, one fresh session per task, budgeted nightly jobs | `sampo run` · `sampo night` |
| **Learns a fix once** | A failure that was fixed before gets its fix offered next time, in any project | `sampo fix list` |
| **Changes many spots** | Batches of exact replacements, applied all or none, inside the task's scope | `sampo edit` · `sampo sub` |
| **Improves itself** | Rule changes backtested on your real sessions; self-improvement kept only when evals agree | `/sampo:improve` |
| **Proves migrations, data and releases** | Migrations up, down, up on a throwaway database; data commands on a copy, invariants checked; the release built offline, run from a throwaway prefix, tagged by `sampo release V` in any repo | `sampo prove --migration`, `--data`, `--release` |
| **Runs incidents** | `sampo capture --incident '<impact>' --signal CMD` opens an urgent FIX only when the harm signal is red, keeps a UTC timeline, closes only on the same signal green twice plus one follow-up, and leaves the signal for `sampo sentinel` | `sampo capture --incident` |
| **Checks infra files** | Dockerfiles, compose, Terraform, Helm, Kubernetes, CI workflows, shell, systemd units: each through its own validator on a copy, offline; no validator installed, unproven | `sampo prove --infra` |
| **Finds problems unasked** | Untested code, undeclared imports, SQL built from input, slow loops, flaky tests, stale docs, leaked keys: no model, worst first; proven on a copy | `sampo find --rank [--ladder]` (tools: trusted only), `sampo prove --capture N`/`--docs`, `/sampo:initiative` |
| **Improves itself, per aspect** | Kalevala: a Rune tower per aspect, generations toward one goal you write; a rewrite stays only if it closed more exam scenarios | `sampo kalevala status` · `sampo rune step` |
| **Stays light** | Only the Ward and write validators run in line; every hook call stays under its budget (the chart below) | `sampo doctor` |

</details>

<details>
<summary><b>Plans</b></summary>

- Intake turns a request into briefs: type and tier (S, M or L), acceptance criteria with the commands that prove them, steps, scope and risks.
- M and L work lists the approaches first.
- The queue orders itself, dependencies included.
- Each brief lives in Sampo's state, outside your repo, unless you turn on `sampo sync`.
- `sampo private add '*.home.example.test'` (or a word) keeps a private name out of the repo: the mirror shows `<host>` or `<private>`, a lane that adds one is refused at merge, and `sampo ship` refuses a push that carries one. The terms stay in Sampo's state.

</details>

<details>
<summary><b>Knows your stack</b></summary>

- Over 1,000 stack cards in 29 areas: languages and frameworks, data, ML and LLM apps, infra, cloud, ops, testing, security, architecture, product domains, UX patterns and visual design, games and hardware, building Claude Code plugins, hooks, skills and mods, practice (brainstorming, premortems, experiments) and a style shelf of twenty named looks with measured palettes.
- Each card gives the exact commands that verify the work, a failing-test recipe, the mistakes models actually make, and a review checklist.
- Cards cost no context until they are read. `sampo lib` picks the ones that match your repo's manifests and files, and the session start names them. `sampo lib show fastapi pytest --section verify` reads just the parts you need.
- The first edit of a file a card covers brings that card's pitfalls (its Avoid section, under 700 characters) into Claude's context, once per card per task.
- A card is picked by what the file imports as well as by its type: a file that imports `@tauri-apps/api`, `fastapi`, `tokio` or `github.com/gin-gonic/gin` gets that framework's card beside its language's (at most two an edit).
- The audit brief carries the review checklists of the cards a task used. `sampo lib stats` shows which cards actually reach Claude, `sampo lib inject off` turns injection off, and `sampo doctor -v` counts how often it and the other enforced checks fired this week.
- `sampo lib search` also finds installed plugins' skills, disabled ones included, so a big skill pack can stay off and still be found.
- `sampo lib source add <git URL>` indexes a third-party skills repo read-only: a shallow, markdown-only checkout in Sampo's state, with no hooks, filters, submodules or symlinks, and nothing run.
- `sampo lib source defaults` adds eleven vetted ones (wshobson/agents, trailofbits/skills, VoltAgent subagents, K-Dense scientific skills, morluto/rea, and anthropics' skills, claude-code, claude-cookbooks, claude-plugins-official, financial-services and prompt-eng-interactive-tutorial), searchable next to the cards at no context cost; `--dry` lists them without cloning.
- Each source is pinned to the commit it was added at (`sampo lib source update` moves the pin and names a licence class that moved). Its licence class comes from its licence file: open (MIT, Apache, BSD, ISC, MPL), source-available (any other licence) or ideas-only (none). `sampo lib show` gives ideas-only entries as name and description only.

</details>

<details>
<summary><b>Verifies</b></summary>

- `sampo check` runs the project's gates and reuses a pass on an identical tree. A new test file that is only another open task's red-first test is left out and named, so one task's deliberate red doesn't block another's close.
- A close runs the tests linked to what the task changed and refuses a new failure. Every few closes the full gates run in the background, and a red run becomes one urgent baseline task by itself.
- `sampo task evidence --run` records a command's real exit code and output.
- Test first at edit time: in a fix or feature task that has changed no test and recorded no failing run, the first edit of a source file brings one line saying to write and run the failing test first. Once per task, never a block.
- `sampo task prove` shows a test failing on the start tree and passing now. `--hunks` reverts each hunk to show the tests notice it.
- `sampo task finish` closes a task in one call, but only with fresh evidence and the audits, docs note and lesson its size needs. It counts its warnings in one line first (a leftover `breakpoint()` or conflict marker among them) and ends by saying where the work landed: committed and pushed, or not. Paths you list in a `.fmproved` file (one glob a line) can't change without a test that failed first and passes now.
- The suite runs in CI on every push.

</details>

<details>
<summary><b>Shows you what happened</b></summary>

- `sampo tour --since main` ranks a whole branch, or a night's run, by review risk and says which files are safe to skim.
- `sampo why FILE:LINE` traces a line back to the task and the words you asked in.
- `sampo at T-0042 -- pytest` runs any command as of a task's start.
- `sampo show X` takes a task id, a FILE:LINE or plain words and shows the task, the line's history or related past work.
- `sampo prove --rollup [--days N] [--json]` lists done tasks no run here proved (T-2273, T-2391), by kind: typed, red or inconclusive evidence (a red run counts as proven when the same command ran green later; a `--bench` run is green), a UI file changed with no `sampo shot`, smoke, stories or flow run, a docs claim (a flag, path or command) the task added with no `sampo prove --docs`, infra, migration or data files with no validator run, a PERFORMANCE close with no `--bench` or one inside the noise band, a RESEARCH close citing no saved note, and a hollow criterion. Each row prints its upgrade command, or `waiting` when it needs an install or a grant; with the queue empty `sampo next` names the newest row that can run. The count shows as `unproven: N (sampo prove --rollup) · kind counts` in `sampo status`, `sampo digest`, `sampo rune status` and `sampo louhi scorecard`, and `sampo task finish` lists what the close still needs under "Not verified". A reply that claims a speedup, accurate docs, a working migration, a rendered UI or a release with no run of that kind is held once at Stop; kept anyway, it is labelled unproven for you, and commits and `sampo pr` carry an `Unproven:` line. Each clause of your request is bound to the criterion sharing its words; a clause no green, failable check covers is listed as meaning unproven at the close and in `sampo prove --rollup`. The exam grades it too: proof scenarios count claims proven by hidden checks, the unprovable one labelled, and false proofs; beyond scenarios run non-code deliverables (a migration pair, a CI workflow, README examples, an incident's harm signal). The same rows reach every surface (T-2395): `sampo smith`'s close prints the Not verified lines, the pane's band shows the count and the kind most need, the MCP `proof` tool serves Claude Desktop and other agents, AGENTS.md points other CLIs at `sampo prove --rollup`, and the site's verifying page charts the count per day.
- `sampo digest` sums up the week; `sampo outcomes --readme` gives the track record in one sentence.
- `sampo outcomes` grades every close by whether it survived 30 days (nothing reverted it, fixed it or corrected it), and `--claims`, `--regret`, `--graveyard` and `--seen` read those grades. `sampo ship --chance N` states the chance CI's blocking jobs pass, settled by `sampo ci-watch` within hours (the ship kind in `--judgment`). `--predictions` shows how often Sampo's guesses at your next request and at a task's effort beat a naive baseline; one that trails it over 20 tries is muted (`--unmute KIND`). `sampo round close` tags a round and lists what it closed.

</details>

<details>
<summary><b>Runs unattended</b></summary>

- Drive mode and full autonomy work the queue without stopping; `sampo notify` tells you when it's done.
- `sampo run` works the queue in one fresh `claude -p` session per task (`--max-usd` caps what it spends); `sampo serve` runs Remote Control on a server.
- `sampo scout` finds work nobody asked for: a newer Claude Code to adopt, dependencies with no stack card, doctor failures (filed in Sampo's own inbox), a weekly brainstorm, and up to 3 `sampo find` leads a day in your own projects.
- `sampo night` runs budgeted background jobs: it works the queue under the night's budget, composts finished lanes and proposes merged lessons.
- `sampo goal set|plan|item|link|show|verdict` keeps a big goal as items with done-when checks across days; one `Goal:` line shows in sessions, `sampo smith`, `AGENTS.md`, the band and the MCP `goal` tool until `sampo goal verdict` says finished.
- `sampo draft` has the top queued small task drafted on a throwaway branch while Claude waits; `sampo lane race T` has two builders take different approaches and `sampo lane pick` merges the better one.
- A missing tool gets a note on the spot (`sampo which` searches every shelf, `sampo new tool` builds one) and holds the task until a run gets past it: in Claude Code (PostToolUseFailure), `sampo smith`, Codex, Gemini CLI and opencode. Installs queue for your yes (`sampo ask ID plugin --later`), shown in the band and the read-only MCP `which` and `grants` tools.
- Every M plan names two readings, three or more options with how each fails, and `Choice N (NN%)`, a bet outcomes score; `sampo focus` and `sampo smith`'s planner hold to it. A one-way, ambiguous step parks for you. `sampo outcomes --judgment` sets the stated Brier beside the base rate's, with races (`sampo lane race`) and how often the first choice lost (also the MCP `calibration` tool). It also prints an interim read, labeled as never scored: bets on tasks closed 7 or more days ago, and how many held so far.
- A shipped feature is done when its outcome is seen: `sampo task finish --outcome SIGNAL` (`event:KIND`, `verb:NAME`, `file:GLOB`, `row:PATH`, `metric:NAME`) is probed 1, 7 and 30 days after the merge, each result stamped with its check time, and `sampo outcomes --silent`, `sampo next` and `sampo doctor` flag the ones never seen (a signal nobody can read is `manual`, never passing). `sampo outcomes --census` rates every verb, skill, playbook, night job and hook note over 14 days as existing, used (a builder's own run doesn't count) and effective (a capture, task or decision right after it in the same session); `--capture` files up to 10 of the unused or ineffective ones.
- `sampo ci-watch` follows the CI run of the commit you pushed and turns a failure into a fix task; `sampo pr threads` turns review comments into steps and `sampo pr train` merges approved, green PRs in order after a re-test.
- **The forever loop.** `sampo loop on` keeps Sampo improving without being asked: when the queue and inbox run out, `sampo next` and the Stop drive start the next round instead of ending the run.
  - A round reviews what shipped since the last one, brainstorms past the newest earlier brainstorms, grounds the ideas in the code, queues every one that holds up and builds them.
  - It counts how many ideas the round queued (`sampo loop yield N` says it outright). Two thin rounds in a row (fewer than three each) or `sampo loop off` end it.
  - Usage running ahead of pace pauses it until there is room, and a closed task or a new round counts as progress, so the drive's 50-push cap stops only real spinning. The pane and the band show `∞ round N` while it runs.

</details>

<details>
<summary><b>Learns a fix once</b></summary>

- When a command fails and a later one on the same program gets past it, Sampo keeps the pair with no one asking.
- The next time any session meets that failure, in this project or another on the machine, Claude gets the fix in the failure's context (`Sampo: known fix for this failure … Try that before anything else`), and a command that failed before is flagged before it runs again.
- A look around (`ls`, `cat`, `git status`) or a retry that happens to pass teaches nothing. A code edit between the failure and the pass makes it a code fix, so nothing is kept.
- A fix that keeps missing stops being offered, and a failure met three times with no fix becomes a task to remove its cause.
- Project fixes travel with the repo: `sampo sync on` mirrors them into `.sampo/fixes.md` (an older repo's `.foreman/` moves there once, in one commit), so clones, other machines and teammates' sessions get them through git. A fix that only came with the repo is offered as a hint to check (never as an instruction, never before a command runs) until you adopt it with `sampo fix trust N`. A sensitive project's fixes stay in that project.
- `sampo fix list` shows them, `sampo fix add "<error>" --do "<what works>"` teaches one the hooks can't see, `sampo fix rm N` forgets one. A hook only ever says a fix; it never runs one.

</details>

<details>
<summary><b>Changes many spots in one call</b></summary>

- `sampo edit` takes a batch of exact replacements on stdin (JSON, or `@@ file` / `<<<` old / `===` new / `>>>` blocks) and applies them all or none: each old text must be there once (or be marked `all`), each file inside the task's scope, and each one the Ward would let an Edit write.
- `sampo sub PATTERN REPL --glob G` previews a regex change as per-file counts and writes it with `--apply`. The third edit of one file in a row says so, once.
- `sampo lane max N` sets how many builders work at once (1 to 100); `sampo lane land` lands every finished lane through merge trains of 8.
- When the session that launched a builder ends, `sampo next` names the stopped builder and `sampo lane resume ID` takes it back: its uncommitted files (secret-looking ones left out) become a WIP commit on its branch, the worktree is freed, and a new builder brief merges that branch first. It refuses a builder that may still run, or one recorded on another host, unless you give `--force "<why>"`.

</details>

<details>
<summary><b>Improves itself</b></summary>

- `sampo backtest --rule/--fix/--card` shows how often a change would have fired on your real recent sessions before it ships; `sampo rule add … --moves corrections --by 30` makes a change a trial the digest grades later; `sampo adopt` learns from a repo's history of reverts and fixes.
- `sampo friction` digests Sampo's own friction (`--minutes`: an estimate of the minutes it cost this week), and `sampo burden --by-verb` lists the verbs nobody calls. Lessons come back as tripwires on related work.
- `/sampo:improve` builds candidates in a separate git worktree, runs the `claude plugin eval` suite against the live version, and only proposes changes that score at least as well. You approve every merge, and it can't edit its own Ward, permission settings, rules or eval suite without your authorization.

</details>

<details>
<summary><b>Improves itself, per aspect (Rune and Kalevala)</b></summary>

- Rune runs generations toward one goal you write once (gen-0: your goals, MUST and NEVER lines). `sampo rune draft "<line>"` writes a whole gen-0 from one line to start from, carrying your standing intent; `sampo rune init` saves it and `/sampo:genesis` drives it.
- `sampo rune step` names the next action from the tower's files alone, so a killed session picks up where it stopped. gen-0 is sealed by its hash, and each generation's seed is checked against it word for word (`sampo rune drift`), then a cold reviewer on a stronger model reads only the generation's diff, exam lines, outcomes and friction (`sampo rune review`; each grounded finding becomes a task, a top gap or a dismissal with its reason), then Sampo hunts blind spots from inputs it didn't write (each class found becomes a rule with a check; each generation also seals a prediction of your next three steers and scores every real steer against it), then the gap map is redrawn against the current code and the next generation's Missions are ranked by expected value from it, the exam, survival and the review (empty coverage cells only break ties), then the whole project is swept (`sampo rune sweep`, which also turns red on a grant kind no earlier generation used); a rewrite is kept only if it closed more exam scenarios than its parent (`sampo rune select`), and three reverted generations in a row stop the tower and hand it back to you. `sampo rune exam` grades Rune itself: drafts from "make this great", from nothing and from "super improve this" on generated projects, scored against your gen-0 on a held-out third, beside a plain model.
- Every generation also advances a Moons campaign (`sampo moons`, the moonshots). One catalogue grows toward 100 ideas on a six-tier ambition scale, from "a clever new check" up to "changes what building software means". The strongest model rates each idea without seeing its author's rating, and every extreme and massive idea gets a kill test first. Ideas are built in climbing order, smaller ones that unblock big ones first, and each generation must move at least one big idea forward a stage (`sampo moons rate|order`).
- You shouldn't have to write steers. `sampo louhi steer` measures how well Sampo expands your raw words into a complete standing rule. A stronger model judges each expansion against the rule you actually ended up with. A clause only counts when the judge quotes the expansion's own words, and a third of the cases are held out. Each instruction-shaped message you send is expanded in the background and queued with that held-out score. If the score is under 70%, the expansion is also listed for you to read. A `steer:` correction on that task becomes a new case. The held-out rate is shown on the Rune page.
- Kalevala runs one tower per aspect together: each area of the coverage map with a gap, plus problem solving, memory and efficiency (`sampo kalevala start`). Moons, exam scenarios and your steers stay shared, so a gain in one tower reaches every other. Each round (`sampo kalevala step`) first restarts a tower that stopped climbing and pauses one that drifted; `sampo kalevala watch` shows which are stalled.
- With 10 or more tasks done and the queue drained, `sampo scout` offers Rune once (by itself under full autonomy). It waits on your own yes whatever the autonomy, and a no is never asked again. The site's Rune page shows every generation live (`sampo rune live on`).

</details>

<details>
<summary><b>Stays light</b></summary>

Only the Ward (on tool calls that can change something) and the write validators (after a write) run synchronously; everything else that records runs async or once per turn, and nothing is injected unless it changes what Claude should do next. `sampo doctor` reports each hook's p95 latency, and the pane's hooks tank shows it live (about 20 ms here).

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/chart-hooks-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/chart-hooks-light.svg">
  <img src="demo/figures/chart-hooks-dark.svg" width="560" alt="Each hook's p95 time per call against its budget, from plugin/tests/bench_hooks.py">
</picture></p>

</details>

## Where you see it

**In Claude Code**, the `sampo-ui` mod draws Sampo live: the band above the prompt with the task, its stage rail and step bar and a live tree of every call; compact rows in the chat; `/fm` for the dashboard; a true-color status line below.

<p align="center"><img src="docs/assets/screens/claude-code.png" width="960" alt="Claude Code with the sampo-ui mod, mid-task on a seeded bug: the chat as a tree of compact rows (a failing assert, the one-line diff, the new test), the band showing FIX small T-0001 verifying with its stage rail and step bar, a live tree of this session's calls with time bars, and the status line with model, context, spend and the task"></p>
<p align="center"><sub>A real session, recorded by <code>demo/record_live.py</code> in a throwaway repo (<a href="demo/live.mp4">the whole 30-second run, MP4</a>).</sub></p>

**In a terminal**, `sampo smith` runs the same loop on any model and draws it: the stations from plan to close, each check, what changed, time and tokens per call.

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screens/smith-tui-dark.svg">
  <img src="docs/assets/screens/smith-tui-light.svg" width="880" alt="sampo smith tui after a closed run adding a --json flag to todo.py: plan and three steps checked off on the track, the activity log, three passing checks, the changed files as bars, a time chart per step and tokens per call">
</picture></p>

**Everywhere else**, every view reads the same `sampo … --json`: the Board above, Sampo Desktop for every project and session in one window, and `sampo mcp` for any MCP client. Other agents (Codex, Gemini CLI, opencode) run the same Ward and context.

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="site/assets/diagrams/architecture-dark.svg">
  <img src="site/assets/diagrams/architecture-light.svg" width="880" alt="How the pieces fit: Claude Code, other agents, Smith and headless runs call Sampo's hooks and the sampo CLI, which keep state on disk; the sampo-ui pane, the status line, Sampo Desktop and MCP clients read sampo's JSON">
</picture></p>

<details>
<summary><b>The sampo-ui mod in detail</b></summary>

**The card.** Above the prompt sits a card showing:
- the task's type chip, id and title, and its stage on a mini rail (●━●━◉─○─○);
- a true-color gradient step bar that animates while Claude works;
- audits, and one dense line of the work: a SAMPO chip, then the counts as pills (done, building, to merge, queued, inbox) and today's meter;
- what waits on you;
- speedrun splits: each stage's time against your typical for that kind of task, green under and red over;
- when idle, one line on what Sampo learned this week (fixes that worked or missed, rules to propose);
- `[f]` dashboard and `[n]` next.

The mod wears the live page's Board look by default: its near-black navy, dim rules and pastel-neon accents, generated from the same `board-tokens.json`, chips and pills in the band and the chat. Set `look: 'forge'` for the anvil-fire look before it; `palette` takes the same words as `sampo code`'s: `'cb'` for a colourblind-safe ok/error pair (errors keep their ✗), or `'riso'` for Smith's risograph inks; those keep coloured words without blocks behind them. In the history press `b` to step back in time and `l` to return to live.

**The chat.** Every row reads like a section of the pane: a mark, a chip naming the kind on its accent (RUN, EDIT, READ, AGENT …), a bold label in the row's colour, its subject, a rule fading out in that colour and the facts on the right.
- Your prompts (`❯ You`), Claude's replies (`● Claude`), commands, edits, subagents and every other tool (skills, MCP calls, artifacts …) get one. A tool's result of up to three lines sits in its row; a longer one keeps Claude Code's own block under it, which expands.
- While a call runs, a comet runs along its rule beside the elapsed time. A command shows its lines, its output (failures first when it failed), the files it changed and a commit.
- An edit's diff keeps a line of context either side of each change, breaks between hunks with `┄`, and washes only the words that changed in a changed line, never the whole width.
- A review's findings list each one with what came of it (fixed, skipped), where it is and the short claim.
- Successful `sampo` bookkeeping commands fold to one quiet `⚙ sampo …  ✓ …` line, while failures keep their full row; finished reads and background-task notifications also fold to one quiet line.

**The chat as a tree.** Each turn is one tree: your prompt at the root, every call Claude makes a branch (`├─`) on a rail down the left, its replies on branches too, and the turn's closing line (`└─ ✻ worked 22s · 6 tools · 1 edit`) the tip.
- Finished calls are compact, so the chat scrolls far less: a passing command shows its description, one line of the command and a line count; a failure shows its failures in full; a diff shows four changed lines and a count of the rest; reads and searches sit on one line.
- The `chat` setting `full` brings back the long rows.

**Turns.** The working spinner names the Sampo step it's on. The line closing each turn says what the turn did (`4 tools · 2 edits +30 −4 · ✓ T-0012 step 3/5`). When Claude stops with Sampo work waiting, the prompt offers `/sampo:next` as ghost text (never while a question waits on you). The footer says when Sampo needs you and shows `full auto` and `drive off` as modes.

**Context.** The band carries a context meter. At 85% Sampo checkpoints the task once, so a `/compact` resumes at the exact step. When a task closes with context at 40% or more, the mod compacts right there: it keeps your standing requests and live decisions, drops finished work, and Sampo's resume note restores the exact point. The threshold is the mod's `freshAt` setting; 0 turns it off.

**Your project's own timings.** Sizes read as words (`FIX · small`, `· medium`, `· large`) and carry this project's history:
- The band shows how long you've been on the task and what that type and size usually takes (`12m on it · usually ~25m`): the median from first focus to done, once three such tasks have closed.
- The band shows the whole queue (done today, queued, in the inbox, what's next), so it never looks nearly finished while work waits.

**Prompts and background work.**
- Every Sampo permission prompt gets a plain-words note under it saying what a yes grants and why.
- When Claude waits on background shells (started in the background, or moved there with ctrl+b), the band says so (`◷ waiting on 2 background shells · cargo test · 3m 12s · Claude picks up when they finish`) and the pane lists them. Each leaves when its notification arrives or it is stopped.
- Coming back to a long unattended run, the band tells you what closed since your last message (`✔ since your last message: 3 done · …`) until you write again.

**The mascot.** The pane's top-right corner has Claude Code's own Claude from the welcome screen. Its `mascot` setting picks blue, orange, purple, green or off. It dances while Claude works, blinks while idle and cheers when a task closes, with a mini Claude in its own color beside it for each running subagent.

**The dashboard.** `/fm` (or `[f]`) opens one panel beside the chat that fills the dock exactly, in the Board's language.

It opens on **the board**: the Board's top bar over its panels, each a picture with the Board's chrome (a numbered chip, an UPPERCASE name, the web's one-line subtitle where the panel is wide enough to hold it whole, a label, corner brackets, a footer with its stat).
- **The top bar**, framed like a panel: the SAMPO mark, the project and how it runs, and the work as counts (done, building, to merge, queued, inbox), numbers bright and words dim; the task in focus as the NOW strip (its kind, id, title, stage, a thin meter and the time); and, only when something needs you, one amber callout with its actions as key caps (`a` approve, `s` show).
- **The grid**: three columns on a very wide dock, two from about 84 columns, one below, a blank row between rows. A last row with fewer panels widens them to the full width, so no slot is ever empty. Fills are drawn in half blocks (two pixels a cell) and dots and hairlines in braille (eight a cell), so clouds and curves stay fine.

The panels:
1. **Lanes:** each lane a hub under its id, the files it holds as named leaves on hairline spokes, its state and steps under it; a file two lanes hold is lit in both and joined by a dotted line (lanes sharing files sit side by side, so it runs level); a building lane sends a packet down its spokes.
2. **Merge train:** each waiting lane a box (its id, its title) pouring a hairline into the gold gate (`gate · running` while a train runs), out into `foreman/build` with what landed in the last day, and under it the lanes landed, newest first, a ✓ and the time each; packets ride the lines while a train runs.
3. **CPU:** the machine by kind of work, as `sampo cpu` splits it (tests, claude, models, hooks, other): the cores busy now as a big figure, its share and whether it's over or under the goal, each kind's cores, and a stacked bar a minute of the last 30 under the goal's dashed line.
4. **The loop:** the words you asked ❯❯❯ the brief they became (its title, `[x]`/`[ ]` criteria, `✓`/`▸` steps), over its progress and the stages.
5. **Library:** a dotted globe, a dot per kept card or moon, over the cards and moons kept each hour of the last day.
6. **Models:** who called which model in the last five minutes, a line a pair as thick as its calls, the models under their families.
7. **Sampo:** the newest `sampo` commands run here, over the queue as a table with status pills; past its rows, a last row says how many more.
8. **State:** the state folder as a tree as wide as its names, each entry's time (and size where there's room), the newest lit, beside the latest decision as a card titled with its file and date.
9. **Sessions:** every live Claude Code session on this machine, any project, orbiting the machine: a ring gauge of its context in its project's colour, its task and its model; idle ones dimmed.
10. **Tests:** a tile a test module, cached, running, failing or idle (an outline), the tiles sized to fill the panel, a count of each above.

The footer's controls are key caps too (`p` details, `n` next, `d` drive, `u` autonomy, `m` mute, `r` refresh); with the pane focused (ctrl+x tab) the key presses it. Where the dock can't hold every panel it scrolls (the arrows, while the pane has the keyboard), and the panels what's happening makes most relevant come first: lanes, the train, the task, models being called. Only the panels in view draw, and a picture redraws only while something in it moves. Library, Sampo, State, Tests and the CPU by kind read `cache/board.json`, which a background `sampo ui --board-cache` refreshes at most every 30 s (it reads /proc twice a second apart and keeps a row a minute).

`p` (or *details*) turns to every section and back, and the pane remembers which. On the details page each section is a panel with a numbered chip on its accent, an UPPERCASE name and what it does, a label on the right, corner brackets and, on a tall dock, a footer with a caption and a stat. Each says only what it must and draws its numbers as pictures; the lists take the rows they hold and History takes what's left, so nothing jumps as work comes and goes; on a short dock the least important panels give way. Top to bottom:
- **The head:** project, mode and the mascot; a bar of the work, done today against what's left (now, queued, inbox, blocked); and the one thing that needs you, most urgent first: a paused Sampo hook, a question waiting for your answer, a yes Sampo is waiting for, a plan waiting for approval (✓ approve, or *show* to read it), a task that just closed (a green sweep, grade, proof and time), failing gates, halved spend caps, a decision due for another look; otherwise *nothing needs you*.
- **The task:** type, id and size, criteria proved and time on it against the usual, the title, a bar with a segment per step (*3 of 5 steps done · 2 left*), the stage rail from plan to close, and the steps around the current one. With nothing active: what's next and how many closed today, or a plan waiting for your yes. <!-- rule -->
- **Lanes**, while builders are out: a node cluster per lane (its hub, a node per step, lit when done) joined by a dashed link, its id as a pill and building, waiting, parked or merged under it.
- **Merge**, while lanes wait: each waiting lane with a mini bar runs into one line to the build (`▶ ▣ build`), the merged ones listed beside it.
- **Tools**, once the session has made a call: one braille lane per kind of tool call (edit, run, read, agent, skill, web) plotting the session's calls over the last 30 minutes, each ending with what runs there now or the last thing it touched.
- **Agents**, while there are any: a node graph rooted at this session, with a branch (`├─`, `└─`) to each subagent (running, or finished in the last half hour), each brainstorm and each lone git worktree, and a builder's worktree under its agent. Every node's bar sits in one column on a shared axis, with a dot where a tool call or an answer landed, and ■ stop for a subagent quiet ten minutes.
- **Queue:** a table (ID, a status pill, kind, title): lanes building, plans waiting for your yes (each with ✓ approve), the queue, the inbox with Start and Drop, and a capture box (Enter runs `sampo capture` without interrupting Claude).
- **CPU**, from the samples `sampo cpu` keeps: the Sampo sessions' share of the machine over the last half hour as stacked columns, one colour a session, against the dashed goal line (10%, or `sampo cpu goal`).
- **Checks:** a cell per gate, green or red, with what fails.
- **Sessions**, when there are others: each with its context and task, and one steer for all.
- **History:** an area of the events over their span, then what happened, newest first, one line each, red for a failure and green for a pass or a close.
- **Closed per day** for 14 days beside **Today**: four tanks for context, spend against the day's cap, hook latency and gates passed.
- Next, Drive, Autonomy, Mute and Refresh.

**The live tree.** The band above the prompt stays put while the chat scrolls, so while work runs it holds one tree of everything happening now.
- This session sits at the root (its task and step, a comet along its rule) with the last thing Claude said it is doing and its latest calls; then a branch per subagent with its own words streaming in, its worktree if it is a builder, and its latest calls, each spinning while it runs, then ✓ or ✗ with its time.
- It keeps one height for the whole turn, so the chat above never shifts as calls come and go, and every node has a time bar in one column, as in the pane's Agents graph. When it runs out of room, words go first, then older calls, then the least recent agents fold into a count.
- It stays two minutes after the last call, then shrinks to one line for the session. With the panel open the band shows only that tree.
- The charts move at about 30 fps while Claude works and stand still when idle; the `motion` setting picks `full`, `calm` (8 fps) or `off`. Toasts mark finished steps and tasks, needed yeses (with a soft chime) and Ward refusals.
- The mod only renders `sampo ui --json` and runs `sampo` on your presses; rules and gates stay in sampo and the classic hooks.

**The status line.** Under the prompt is one clean true-color layout:
- a session line: model, folder, branch, context and rate-limit bars, cost, lines changed;
- a Sampo line: task, stage, step bar, audits, queue, autonomy, Ward.

When the mod's card is above the prompt, the Sampo line shrinks to a chip on the session line. `"statusline_layout": "stack"` in the install manifest brings back the old stack of your original line, claude-hud and the Sampo lines. `NO_COLOR` set drops the colour from Sampo's own lines. In tmux, `set -g status-right '#(cd #{pane_current_path} && sampo state --line)'` puts the same task, step and queue in the status bar for the pane you're in (it never creates a project).

Also: per-subagent rows, terminal titles and desktop notifications, `sampo watch`, and an optional OpenTelemetry → telegraf → InfluxDB → Grafana setup in `plugin/observability/`. `/tui fullscreen`, `/focus` and `Ctrl+O` help too.

</details>

<details>
<summary><b>Other agents, MCP and the desktop app</b></summary>

**Other agents:** `sampo agents install codex|gemini|opencode` puts Sampo into Codex, Gemini CLI or opencode: its Ward, per-turn context, `sampo mcp` server and a short rules block. They run through the same hook Claude Code runs (`hook --agent X EVENT`).
- The adapters are built to each tool's documented hook format and tested against recorded payloads, but not yet verified against live CLIs: check that a refused command is refused there before relying on the Ward.
- `sampo agents list` shows what's wired, and `sampo agents uninstall X` takes out exactly what install added.
- Codex also gets the drive and done-gate (its Stop hook).
- Two things stay Claude Code only: Gemini's AfterAgent would discard the answer it blocks, and the `sampo ask` permission prompt needs Claude Code to show it.
- A config with comments (JSONC) is left alone with a note.
- The files that load the hooks are protected core, like Claude Code's settings: `~/.codex/hooks.json` and `config.toml`, `~/.gemini/settings.json`, and opencode's `opencode.json` and `plugins/`, including their project-level copies. An agent that edits them, or runs `sampo agents uninstall`, needs your yes.

**MCP:** `sampo mcp` serves the project read-only to any MCP client: its state, next action, recall, research and briefs, the stack library (`lib_suggest`, `lib_search`, `lib_show`) and the instruments (`outline`, `sym`, `impact`, `why`, `fail` on given text, `logs`, `data`, `trace`), `find`, `exam_scorecard` (Rune' done gate) and `genesis_status` (the tower's stage and next action).
- Paths must resolve inside the project, values can't start with `-`, each is length-capped, each call has a timeout, and everything sent is redacted. `sampo mcp register claude` prints the `claude mcp add` line; `sampo mcp register desktop` prints the `mcpServers` stanza for Claude Desktop's config (the sampo binary by absolute path, the project by `-p`), so Desktop reaches `find` and the rest.
- `sampo mcp new NAME --wrap 'CMD' [--sub a,b]` writes a stdlib-only MCP server for any CLI, plus a test that drives it over stdio. The server runs argv, never a shell; it has a timeout and an output cap; and it refuses to wrap destructive commands (`rm`, `git push`, a shell…). Register it with `sampo mcp register claude DIR`.

**Knowledge out:** `sampo recall --export-rules` writes the lessons from finished tasks to `.claude/rules/sampo-lessons/`, one rule file per task scoped by `paths:` to the files it touched, so Claude Code loads a lesson only with its files, even without Sampo. `sampo decide --export-adr DIR` writes one ADR file per recorded decision. `sampo task defer ID --until "when PATH changes"` brings a task back in `sampo next` once that file changes.

**Desktop app:** [Sampo Desktop](https://github.com/ChaseSunstrom/foreman-desktop) (Tauri 2 + Svelte 5; `install.sh --desktop` installs its newest release) shows every project, agent session and device in one animated window.
- Live project dashboards with Start, Drop, Approve-plan, capture, drive and autonomy controls.
- It starts, watches, messages and stops sessions for Claude Code, Codex, Gemini CLI and opencode.
- It opens every Claude Code session on every device: terminal, Remote Control and headless. For each, it shows the conversation with images, subagents and their transcripts, and scratchpad files and pictures, and it can continue the session.
- A Remote control page, a Ctrl+K palette, and agents and devices pages, with your tailnet's machines suggested.

The app never talks to a server: it runs `sampo … --json` on this machine, and on other devices over ssh. Its live views come from `sampo projects --follow`, `sampo ui --follow`, `sampo session list --follow` and `sampo session tail --follow`.

</details>

## Every harness: headless, other agents and Smith

### Headless on a server

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/headless-on-a-server-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/headless-on-a-server-light.svg">
  <img src="demo/figures/headless-on-a-server-dark.svg" width="880" alt="Headless on a server: a table of 4 rows (`sampo serve`, `sampo run`); its text follows">
</picture></p>
<details><summary>As text</summary>

| | `sampo serve` | `sampo run` |
|---|---|---|
| **What** | Claude Code Remote Control as a systemd user unit | The queue in fresh `claude -p` sessions, one task each |
| **Start** | Ask Claude to "serve this repo" (one yes grants `remote`) | `sampo run` (`--max-usd` caps the spend) |
| **You** | Send requests from claude.ai/code or the Claude app | Come back to closed tasks |
| **When it fails** | Restarts; stops after five failed restarts in ten minutes | Waits out a usage limit; blocks a stuck task with the reason |

</details>

<details>
<summary><b>Serving, sampo run, nightly runs and the sandbox</b></summary>

**`sampo serve`.** In a repo on your server, ask Claude to "serve this repo". Claude asks you once (a yes grants `remote`), then `sampo serve` starts Claude Code Remote Control there as a systemd user unit, and the project switches to full autonomy with drive on.
- The unit survives logout and reboot: it turns on linger, or says why it couldn't.
- It restarts on failure. After five failed restarts in ten minutes it stops, and `sampo serve status` or `sampo doctor` says why. It doesn't wait out usage limits.
- Send feature requests from claude.ai/code or the Claude app whenever you like. They're captured, planned, built, audited and committed one after another.
- "Stop serving" (`sampo serve stop`) undoes it, and uninstall stops every unit.
- Claude Code has to trust the repo first: open `claude` there once.

**`sampo run`** works the queue in fresh `claude -p` sessions, one task each, so a long queue never piles up in one context.
- It waits out a usage limit (up to six hours by default) before retrying. A task that makes no progress is blocked with the reason, so the next run moves on.
- The machine is kept from sleeping while it runs (systemd-inhibit, where there is one).
- Self-running: `sampo state`, the pane's band and `sampo smith` carry one line of what ran or waits by itself (triggers fired today and their routes, installs waiting for your yes, lanes recovering, self-fix trials due, how the next-request predictions do against their baseline); `sampo triggers` has the table and the MCP `triggers` tool the same, as JSON.
- To run `sampo night` every night and `sampo night --tick` hourly, `sampo night --stage` writes both systemd timers into state and queues one request for your yes (`--watch-ci`, `--watch-issues OWNER/REPO` add those lines); after the yes `sampo night --install` starts them, `sampo night --uninstall` removes them.

**The sandbox** is opt-in: `sampo run --sandbox`, or `sampo sandbox on` to keep it for later runs and `sampo night` timers (`--no-sandbox` skips it for one run).
- Each session then runs under Claude Code's own sandbox (bubblewrap with socat on Linux, Seatbelt on macOS): Bash writes only the checkout, the cache dirs, Sampo's state and the git dir.
- It reaches only the allowed domains (`sampo sandbox allow DOMAIN`; default: the git remote's host and the package registries for the project's languages).
- `sampo run` refuses to start when the host can't run the sandbox. `sampo outbound ID` lists the hosts and queries a task's sessions reached.

</details>

### Smith: any model, Sampo's loop

`sampo smith "make calc.add add"` runs Sampo's loop without Claude Code, on whatever model you have: a local server (Ollama, LM Studio, llama.cpp, vLLM), a profile in `$FOREMAN_HOME/models.json`, or a cloud whose API key is set (`sampo smith models` lists them).

Or it hands each step to a coding CLI you already have: claude, codex, gemini or aider.

Every edit and command goes past Sampo's Ward first, the harness runs each check itself, and the task closes and commits only when they pass. Ask in plain words:

- A bug: `sampo smith "total() in cart.py ignores the quantity" --worker claude` writes a failing test, fixes it, proves both.
- A feature: `sampo smith "add a --json flag to todo.py, with a test"`.
- A change across files: `sampo smith "rename fetch_user to load_user everywhere"`.
- Docs: `sampo smith "document every environment variable config.py reads in README.md"`.
- A question: `sampo smith "what does parse_dates do with a date that has no timezone?"` answers from the files, citing them, and changes nothing.
- A new project: in an empty `git init` folder, `sampo smith "make a Python CLI that counts lines, words and characters, with tests"`.
- The app: `sampo smith` on its own opens it, the way `claude` does, and `sampo smith tui --tour` plays a 30-second guided run.

Real runs of each kind are on the [Smith page](https://chasesunstrom.github.io/sampo/features/code.html#ex-fix), recorded in `demo/examples/`.

<details>
<summary><b>How a run works</b></summary>

- The harness does the stations itself: the model writes a short plan (checked, and sent back to fix when it's malformed), then picks one action at a time for each step.
- Every edit, file write and command goes past Sampo's Ward first, so a refused `rm -rf ~` never runs; the model reads the refusal and tries another way.
- The harness runs each criterion's check, hands a failure back to the model (three rounds, three more on `--escalate MODEL`; a type and tier whose own closes keep needing later fixes starts on it), and closes and commits the task, or blocks it with the reason.
- `--dry-plan` shows the plan and changes nothing. `--no-commit` closes without committing. `--json` prints one event per line.
- With no model or API key set up, Smith uses the Claude Code CLI if you have it (it says so when it starts); `--worker codex` (or `gemini`, `aider`) picks another: that CLI plans in its read-only mode and does each step, and Smith still makes the task, runs every check before and after, and commits only when they pass.
- Any language: the checks are the project's own commands (`npm test`, `cargo test`, `go test`, `pytest`, a one-line assert).
- A run that was killed or lost its model carries on with `sampo smith --resume`.
- `cat error.log | sampo smith "fix the crash in this log"` reads what's piped on stdin as the request's context (up to 256 KB).
- claude's own flags work where they mean the same thing: `sampo smith -p "fix add"` (`--print`) is `--plain`, `-c` (`--continue`) is `--resume` (the newest unfinished run), and `--output-format json` is `--json` (`text` and `stream-json` are refused). sampo's `-p PROJECT` goes before the verb (sampo, then `-p SLUG`, then `smith …`), or `--project` after it.
- While a coding CLI works, each file it reads or edits and each command it runs shows as it happens (claude's stream; `--json` gives `tool` events), and a quiet turn says it's still working every 10 s.
- `sampo smith why [ID]` explains a run (the newest with no ID): how it ended or that a kill stopped it, the steps done, a stall's cause, and the one command to run next.
- In a repo marked sensitive, only a model on this machine is used.

</details>

<details>
<summary><b>Workers and runners</b></summary>

- `sampo smith "FIX: make calc.add add" --worker codex --verify "python3 test_calc.py"` hands each step to a CLI you already have (`claude`, `codex`, `gemini`, `aider`, or your own `'CMD {prompt}'`), one unattended turn at a time, killed after `--worker-timeout` seconds (1200). `--verify` plans without a model.
- Only codex runs shell commands, inside its own sandbox. It and the verify get no forge, cloud, registry or other credentials, and a sensitive repo is refused.
- After each turn the harness kills what the worker left running and puts back anything Sampo refuses (a protected file, ignored or not, a planted git hook or config, your shell startup files, other agents' settings and the MCP servers in `~/.claude.json`) and says so. The worker runs as you, so Sampo judges what it wrote, not what it read or sent; its own verify decides, never the worker's word.
- `sampo smith runners` lists every coding CLI Smith knows (claude, codex, gemini, aider, opencode, qwen, cursor-agent, goose, amp), which are installed, which can plan read-only, the one a run with no flags uses, and every runner's record. `--check` asks each for its version and names any configured model id its provider has retired, with the replacement.
- `sampo smith runners --use codex` makes it the default; add `--here` for this project only (a name, never a command; `--use ''` clears it).
- `sampo smith runners --ping [NAME]` asks each installed runner one tiny question through its read-only plan command and says how it answered (ok, not logged in, usage limit, flag refused, endpoint down, no answer), how fast, and its version; it spends a few tokens a runner.
- Your own CLI goes in `$FOREMAN_HOME/code.toml`: `[runners.mine]` with `work = "mycli {prompt}"` (and `plan = …` for its read-only mode), then `runner = "mine"`. Only claude's read-only mode has been verified to only read; another plans only when you name it, with a note, and the run stops if its plan changes a file.
- A template that turns its CLI's own approvals off (`--yolo`, `--dangerously-skip-permissions`, `danger-full-access`, `--approval-mode yolo`, …) is refused until you add the `[ack]` line the refusal prints, which holds that exact template's hash: change the template and it needs a new one.
- `runner = "auto"` in code.toml: Smith plans with the usual runner, then gives the work to the runner with the best record on that kind of task (FIX, FEATURE, …): 3 or more past runs, two thirds closed, the least spent per closed run.
- When a worker turn fails: a usage limit that resets within `limit_wait` seconds (code.toml, default 3600) is waited out and the turn done again; past it the run stops and says when to resume. With `chain = ["claude", "codex"]` in code.toml, a crash, timeout or signed-out runner hands the turn to the next one.
- The claude sessions Smith starts run lean: none of your plugins, MCP servers or settings hooks load there, only Sampo's Ward, and the planner also has no tool that runs code (`--restricted`). On this machine that halved a planner call's input (31,620 → 15,286 tokens); `python3 plugin/tests/e2e/lean_probe.py` measures it and checks the Ward still refuses. `lean = false` in `$FOREMAN_HOME/code.toml` launches them as before.
- A lean worker whose session starts with an MCP server, an `mcp__` tool or a plugin that isn't built in is stopped at its first line and the run blocks, naming them. Inside a run, a coding CLI's `sampo` only reads: every command that changes Sampo's state, grants above all, is refused there.
- `sampo smith bench --model qwen2.5-coder:32b` (or `--worker codex`) grades a model or CLI as a Sampo worker on five seeded tasks in throwaway repos: a seed counts only when its own check passes afterwards, the dangerous one must leave its canary file alone, and the score is kept in `code-bench.jsonl` so runs can be compared. `--seeds operator,rename` picks seeds, `--seed-timeout` (600 s) and `--max-steps` (40) bound each one, and `--json` prints the run.

</details>

<details>
<summary><b>The app, settings, costs and hooks</b></summary>

- `sampo smith` on its own opens the app: inside a project, that project's active task, queue, inbox, blocked tasks and past runs; `Esc` (or `p` from a run) lists every Sampo project on the machine, newest first, with what each is doing.
- `Enter` or `r` on a task runs it there (`sampo smith --task ID`: the task's own criteria and steps are the plan, no planner call; one run per task at a time), `Enter` on a past run replays it, `n` starts a new request in that project's folder, and `m` picks the runner.
- On a terminal, `sampo smith "make calc.add add"` opens the app with that run going, the way `claude "<prompt>"` does; `--plain`, `--json`, `--dry-plan` or a pipe keep the line-by-line output.
- `sampo smith tui "make calc.add add"` runs the same loop in a terminal UI: a track of the stations, check chips, change bars, a time chart and tokens per call, with the diff (`d`), past runs (`h`), the model or worker picker (`m`) and the settings (`,`) a key away.
- It draws in 24-bit colour and moves: the working station pulses, counters and bars ease to their values, a Ward refusal flashes and the close sweeps the track (`motion = reduced` or `off` stills it; `--record run.mp4 --events RUN.jsonl` films a run, [demo](https://chasesunstrom.github.io/sampo/features/code.html)). `z` hushes motion to reduced, then off, then full again, for the Claude Code pane too.
- After a run closes, `u` twice undoes it (a `git revert` of its commit, marked undone in past runs) and `P` shows its pull request text from `sampo pr`, which Enter posts when the task holds the publish grant.
- During a run, `n` asks about the code beside it, and Tab turns that box into a ticket for after it: tickets ride a rail under the track and start one by one as each run closes (`T` reorders or drops them).
- When a run stalls or stops, `x` on its card hands the task to a live CLI in this terminal, with the task's brief as the opening prompt; when you exit it, the app re-runs the task's checks through the Ward (`sampo smith --recheck ID`) and says whether it can close.
- Before a project's first run its welcome shows what a run needs (sampo on PATH, a runner, the folder as a project), each missing one with the key that fixes it, and `e` asks "explain this repo". The app's header names what holds the run in: Landlock (with its ABI) or Sampo's Ward only. The runners screen (`,` then `r`) shows each one's last answer as a lamp, and `p` pings the selected runner.
- `sampo smith tui --tour` plays a 30-second guided run with a caption naming each panel as it comes alive; `--tour --snapshot FILE.svg` writes it as one animated SVG, no video tools needed ([the tour](https://chasesunstrom.github.io/sampo/assets/code-tui-tour.svg), drawn from today's code at each site build).
- Themes: night, paper, signal for colour-blind eyes, mono, and `theme = "riso"` for risograph inks on a paper stock (`palette = "riso"` puts the inks on any theme, and on the pane). On a terminal that draws a half block one column wide, the built track stands up as a lit rail; `?` shows the keys as a poster.
- `sampo smith tui --print-config > ~/.claude/foreman/tui.toml` gives you its theme, motion, layout and keys to edit. `sampo settings list` shows them beside the Claude Code pane's, `sampo settings set motion reduced` changes one in place, keeping your comments, `sampo settings diff` lists the ones you changed, `sampo settings export` prints them as one share code for `sampo settings import CODE`, and `sampo settings undo` puts the last set or import back.
- Inside the app, `,` lists the settings by group with search, changed-only and `:set KEY VALUE`; a theme, palette or motion change is a preview until a key keeps it, and goes back after 10 s without one.
- A Python file in `~/.claude/foreman/tui/` can add a panel or a command (see `plugin/templates/tui/cost_meter.py`); one that fails or runs slow (a panel over 50 ms, a command over 2 s) is switched off with a note, never taking the screen down.
- `sampo smith config` lists every Smith setting with its value and where it came from (a flag, the environment, the project, code.toml or models.json, or the default); `--explain KEY` says what one does and how to change it. A code.toml that doesn't parse is named with its line.
- What a run costs: a claude CLI's own count, or a models.json profile's `"price": {"in": 3, "out": 15}` (USD per million tokens), goes to `sampo budget`'s ledger call by call and onto the run's close line. `--max-usd 2` stops a run once it has spent $2 (a resume keeps what it spent), and each claude turn gets `--max-budget-usd` with what is left.
- Run events to your own tools: in `$FOREMAN_HOME/code.toml`, `[on]` with `closed = "notify-send done"`, `blocked = "…"` or `needs-you = "…"` runs that command with the event's JSON on stdin, past Sampo's Ward, 30 s at most. `sampo state --line` ends with the newest run (`code T-0007 closed`), for tmux and prompt segments.

</details>

### Model layer: models advise, proof decides

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/chart-models-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/chart-models-light.svg">
  <img src="demo/figures/chart-models-dark.svg" width="560" alt="How often each model picks a task's type right on projects it never saw (leave one project out), from research/models/bench.jsonl">
</picture></p>

<details>
<summary><b>Rule first, then local, then cloud; live only by the exam</b></summary>

Some calls can go to a model: a request's type and size, whether two items are the same, whether a lead is real. Sampo asks a deterministic rule first, then local models (Ollama, LM Studio, llama.cpp, vLLM), then cloud ones, and never a cloud model from a sensitive project. Every answer is booked with its probability.

- Each use starts in shadow mode: the rule decides and its call is logged; the hourly tick has a local model answer the same questions. The exam replays both against your closed tasks at zero tokens.
- A use goes live only by a `model-live` decision citing the exam run that showed it beating the rule. No Ward or evidence gate ever asks a model, and the layer downloads and installs nothing itself.
- A server you own goes in `$FOREMAN_HOME/models.json` with `"own": true` and its key named by `key_env` (never the key itself): it is asked after loopback servers and before clouds, and the night's shadow pass may use it, but never for a sensitive project. `TYPESAFE_BASE_URL` (with `TYPESAFE_MODEL`, `TYPESAFE_API_KEY`) adds a Jev-compatible server the same way.
- Other agents ask the same layer through the `decide` MCP tool; their answers are booked apart from Sampo's own. `sampo doctor`'s model backends line lists what it can reach and which uses are live; the site's Model layer page shows the newest exam replay.

</details>

## When not to use Sampo

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/when-not-to-use-sampo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/when-not-to-use-sampo-light.svg">
  <img src="demo/figures/when-not-to-use-sampo-dark.svg" width="880" alt="When not to use Sampo: a table of 6 rows (Skip it for, Why, and what instead); its text follows">
</picture></p>
<details><summary>As text</summary>

| Skip it for | Why, and what instead |
|---|---|
| **Quick one-off questions or scripts** | Planning, evidence and audits are overhead for a five-minute job; even an S task has a brief. Plain Claude Code is faster. |
| **Native Windows** | Sampo needs Linux, macOS or WSL. |
| **Approving every command yourself** | It's built for bypass mode with the Ward as the brake. It works in the default mode too, but its drive keeps stopping at prompts. |
| **Tight token budgets** | Brainstorms, second plans and audits run extra model sessions (capped by `sampo budget`). Turn them down, or skip Sampo. |
| **Installing no plugins** | [`plugin/templates/CLAUDE.lite.md`](plugin/templates/CLAUDE.lite.md) is the discipline in under 40 lines for a CLAUDE.md, with nothing enforcing it. `sampo bench duel --vs snippet` measures what it's worth. |
| **Code that must not leave the machine** | Mark the repo sensitive (`sampo sensitive on`) so outward tools are refused, or don't use cloud-facing features there. |

</details>

To add the lite rules to a project:

```bash
curl -fsSL https://raw.githubusercontent.com/ChaseSunstrom/sampo/main/plugin/templates/CLAUDE.lite.md >> CLAUDE.md
```

## Documentation

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="demo/figures/documentation-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="demo/figures/documentation-light.svg">
  <img src="demo/figures/documentation-dark.svg" width="880" alt="Documentation: a table of 5 rows (Doc, What&#x27;s in it); its text follows">
</picture></p>
<details><summary>As text</summary>

| Doc | What's in it |
|---|---|
| [`MASTER.md`](MASTER.md) | The system map: how to prompt it, every file, how the parts interact, operations and limitations. Start here. |
| [`INSTALL.md`](INSTALL.md) | Installation steps written for an agent to follow, asking before each change. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release notes. |
| [`BUILD_PROMPT.md`](BUILD_PROMPT.md) | The spec Sampo was built from, including the plugin choices (§4.6 and Appendix A). |
| [The site](https://chasesunstrom.github.io/sampo/) | A page per feature, with recorded runs. |

</details>

## Updating and removing

<details><summary>Upgrading (formerly Foreman)</summary>

**Upgrading.** Sampo was formerly Foreman, and nothing you have breaks: `fm` stays as an alias that works as before and prints one deprecation line a day; the plugins `foreman` and `foreman-ui` are now `sampo@sampo` and `sampo-ui@sampo` in a marketplace named `sampo`; an install made before keeps working under its ids ending `@foreman`, and `sampo migrate plugins` moves it to `sampo@sampo` in one write per file, never both ids enabled (`--dry-run` shows it, `--undo` puts the files back byte for byte), the mod's options with it (the `/foreman:` commands become `/sampo:` and the `foreman:` agents `sampo:`; Sampo still reads the old names wherever they were saved; [INSTALL.md](INSTALL.md) has the details); the `FOREMAN_*` variables and the `Foreman-Task` commit trailer keep working; and `~/.claude/foreman` is migrated once by `sampo migrate` (`sampo migrate --undo` restores it exactly). The renamed verbs keep their old spellings too: `code` is `sampo smith`, `genesis` is `sampo rune`, `exam` is `sampo louhi` and `guard` is `sampo ward`. The GitHub repo is ChaseSunstrom/sampo (GitHub forwards links that use its old name), and the install lines above use it.

</details>

- **Update:** `git pull` in `~/.claude/foreman` (then `/reload-plugins`), or rerun `install.sh`.
- **Health check:** ask "is sampo ok?" (`sampo doctor`). It also names any line in your CLAUDE.md, rules or Claude Code's memory that contradicts Sampo's, such as "never run the tests", with a fix. **Hygiene:** "clean up" (`sampo tidy`).
- **Remove:** `~/.claude/foreman/plugin/uninstall.sh`. It uninstalls the plugin and marketplace and undoes the wiring. It keeps `state/` unless you pass `--purge-state`, and `--dry-run` previews.

<details>
<summary><b>Starting from a clean slate (optional)</b></summary>

`reset-claude.sh` resets only the parts that change Claude's behavior, after backing up `~/.claude` and `~/.claude.json` to `~/.claude-reset/<timestamp>/`. It resets:
- plugins, except the ones Sampo keeps;
- user hooks and permissions;
- the global CLAUDE.md, rules, skills, agents and commands.

Login, MCP servers, history, memory, themes and per-repo `.claude/` folders are untouched. Undo with the `tar` command it prints.

```bash
./reset-claude.sh            # dry run
./reset-claude.sh --apply    # close Claude Code first
```

</details>

## Development

<details>
<summary><b>Tests, recordings and the repo layout</b></summary>

```bash
python3 plugin/tests/run.py                                     # unit + behaviour, one class per CPU slot, shared by every run on the machine (~80 s)
python3 plugin/tests/bench_hooks.py --runs 50                   # hook latency
python3 plugin/tests/e2e/roundtrip.py                           # install/uninstall/reinstall in a sandbox HOME
python3 plugin/tests/e2e/scenarios.py                           # live §12 scenarios (uses claude -p; costs tokens)
python3 demo/record.py [--mp4]                                  # re-record sampo demo (demo/demo.cast, .svg, .mp4)
python3 demo/record_live.py [--from-cast]                       # re-record Claude Code + the mod (demo/live.*; a Sonnet turn)
```

A recording never changes with the code it shows, so `sampo doctor` checks them: its recordings line fails when one under `demo/` or `docs/assets/screens/` shows a retired name (`fm`, the old product name, a renamed verb such as `sampo code`) or when no entry in `doctor.RECORDINGS` says what a new one shows, and warns when one is over 21 days old and the code it shows has changed since (a warning, so the gates never turn red with the calendar). Each line ends with the command that makes the recording again, and Sampo's scout files either one as work.

`sampo release-check` also runs every fence marked `bash runnable` in this file and MASTER.md, each in a throwaway home with stdin closed and a timeout. That is hygiene, not a sandbox: a runnable fence is trusted like the tests and can still reach any file and the network. Mark a fence runnable only when it needs nothing but `sampo` and the shell.

The README's figures are real: `sampo readme diagram docs/assets/diagrams/NAME.json` draws each diagram from its spec as a dark and a light SVG in the Board look; the Board is the live page (`plugin/web/live`) fed the test fixture `plugin/tests/fixtures/live/snap-5.json`, never this machine's state, the terminal from `sampo smith tui --events demo/examples/feature.jsonl --snapshot`, the Claude Code still from `demo/record_live.py`'s video; `sampo readme check README.md` holds them to their files, pairs, sizes and private text. The Ward and architecture diagrams are hand-drawn tiles in `site/assets/diagrams/`.

```
BUILD_PROMPT.md       the spec Sampo was built from
MASTER.md             system map (start here)
CHANGELOG.md          release notes
install.sh            one-liner bootstrap
setup-plugins.sh      curated plugin setup (safe to re-run; --dry-run to preview)
configure-repo.sh     one-time for forks: point everything at your GitHub repo
reset-claude.sh       optional: reset Claude Code's behavior layer before installing
release.py            thin wrapper over `sampo release VERSION [--push]` (--push needs the task's publish grant)
.claude-plugin/       local marketplace "sampo"
plugin/               the Sampo plugin (sampo, hooks, skills, rules, agents, tests, evals)
demo/                 the recordings (Claude Code with the mod; sampo demo), the table figures and the scripts that make them
docs/assets/          the README's screens and diagrams (each diagram's spec beside it)
site/                 the project site; site/assets/diagrams holds the hand-drawn diagrams
local/ state/ backups/   machine-specific, gitignored
```

The reasoning behind the curated plugins, their measured context costs and conflicts are in `BUILD_PROMPT.md` (§4.6 and Appendix A) and `MASTER.md`. Preview what `setup-plugins.sh` would change with `./setup-plugins.sh --dry-run`.

</details>
