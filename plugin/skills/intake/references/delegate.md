# Delegation (reference)

The main thread plans, edits and writes all state. Subagents and fm's child runs are bounded by `sampo budget` (T-0227): use them whenever they buy parallelism or fresh eyes — the caps, not abstinence, keep spend in check (T-0364: "default none" left long gates idle and research serial).

Models (T-0373): the main thread (Opus or Fable) plans, judges, merges and writes state; the work it hands out runs on the cheapest model that can do it, passed as the Agent call's `model` (and `effort` where Claude Code offers it):

| Work | Agent | Model |
|---|---|---|
| find where something is, read a long log/changelog and summarize it | `sampo:fm-scout`, or general-purpose with `model: "haiku"` | Haiku |
| recon, research, audits, debugging hypotheses | `sampo:fm-recon` / `fm-reviewer` / `fm-debugger` | Sonnet (their frontmatter) |
| an S task in a builder lane | `sampo:fm-builder` (`sampo lane brief` prints `model: "sonnet"`) | Sonnet |
| an M task in a builder lane, an L plan's critique | `sampo:fm-builder`, `sampo second plan` | the main model |
| brainstorm lenses, research children, oracle, bench | `sampo ideas` / `sampo research ask` / … (`--model`) | Sonnet by default |

A cheaper model's output is still checked: the main thread reviews a builder's diff, spot-checks two claims of any report, and re-runs the criteria itself.

Allowed only for:
1. read-only recon of a large codebase area → `sampo:fm-recon`
2. web or documentation research → first `sampo recall "<question>"` (it may already be known), then `sampo research ask "<question>"` (T-0206: parallel web-only researchers per sub-question, every quoted claim re-fetched and checked, one note saved; ✗ claims are unverified); `sampo:fm-recon` when the answer also needs the codebase
3. dependency or security audits → `sampo:fm-recon`
3b. a failure that keeps coming back → `sampo:fm-debugger` (read-only; ranked hypotheses with discriminating probes, ready for `sampo task hypo`)
4. optional independent read-only review of L-tier changes → `sampo:fm-reviewer` (or `/code-review`)

5. the one bounded exception that edits — a builder (T-0234) → `sampo:fm-builder`, below

Contract for every delegation:
- A self-contained brief: the question, scope paths, what to ignore, the output format. Never the conversation history.
- Read-only tools only (the Sampo agents have Read, Grep, Glob and, for recon, WebFetch/WebSearch) — except a builder.
- Output ≤ 400 words, every claim backed by `path:line` or a URL, confidence per finding, an explicit "not checked" list.
- Save the summary: `sampo research add <topic> <<'EOF'` … `EOF`. Spot-check at least two claims yourself before relying on them.
- Parallel only with disjoint scopes, at most 3 at once.
- Installed plugins' implementation agents are not used for implementation; their review agents may be used for (4).

Builders (T-0234): an independent S/M task with runnable criteria can be worked by a `sampo:fm-builder` while the main thread does something else (another task, a review). At most two out at once; never two that edit the same files.
1. `sampo lane brief ID` writes the builder's brief (the task's request, criteria with verify commands, steps, its contract) and prints the Agent call: `subagent_type: "sampo:fm-builder"`, `isolation: "worktree"`, prompt `Read <brief> and work the task it describes`. It refuses an L task, a task someone is working on, a task with no verify command and a third builder. `sampo lane brief --ready` does step 1 for every ready S/M task in one call, up to the free slots (`--limit N` caps it; `--json` gives the list for parallel Agent calls), and skips a task whose scope overlaps one briefed in the same batch (T-3293).
2. The builder's first command, `sampo focus ID` in its worktree, binds the task there (and records the branch it's on): the Ward, evidence and gates are its own. It works test-first, records evidence, runs `sampo check --evidence ID` and commits on its branch. Enforced, not only asked: the guard refuses any write outside its worktree but scratch (category `confine`, never grantable; the main checkout never counts as scratch), and from its worktree `sampo task done/finish/audit/drop` and every `sampo lane` change are refused.
3. When it returns: one `sampo:fm-reviewer` on `git diff HEAD...<branch>`; fix or reject findings (in the worktree, or after the merge); `sampo lane merge ID` (T-0377: a --no-ff merge with every changed file judged as the task's own write, so it also works on Sampo's own repo, where the guard refuses a plain `git merge`; T-0948: it first re-runs each criterion's verify command in the worktree and refuses red or a brief with no verify command, so fix the branch first, and `--unverified "<reason>"` waives it from the main thread only, on the ledger); `sampo lane rm ID` (takes the task back and frees the builder slot; the worktree goes, and its own branch — the one recorded at focus, or `foreman/ID` — once merged, never a branch it switched to or a default branch); `sampo focus ID`; re-run its criteria here (fresh evidence on the merged tree); `sampo task finish ID` with the review's lenses — no `--commit`, the merge was the commit. Many lanes: `sampo lane accept ID --audit …` for each reviewed lane instead (T-4286: a lane whose newest affected run at its tip is missing, red or did not hold the four repo-shape classes gets its own check, detached and niced, holding them and, for a merge-commit tip, the whole branch's files; green queues it, red leaves it out with the failing classes on its task (accept says `held: check red: CLASSES`, the land's own words, never "queued"), a cut-off or dead check is started once more, then it is said; `--no-check` only after a passing `sampo check --affected --hold --evidence ID`).
4. A builder that failed or stopped: read its report, `sampo lane rm ID` (it refuses uncommitted work: commit it on the branch or remove it there), and work the task in the main thread. To redo it with a new builder instead, drop the first: `sampo lane rm ID --why "<what went wrong>"` (main thread only; a locked worktree goes, its branch stays), then `sampo lane brief ID`: the new brief's "First attempt (dropped)" section has the reason, the kept branch and the last refused merge's red check (T-1823). A brief that was never launched: `sampo lane rm ID` frees its slot. A lane that came back before its merge (`sampo next` names the cause and the one recovery; in full autonomy it is the order and the Stop drive continues on it): `sampo lane recover ID` commits its uncommitted files as a WIP commit on its branch, frees the worktree, resets the task and writes a new builder brief (with "Why it came back" for a merge conflict or a refused merge) that merges that branch first; launch the Agent line it prints (T-1028, T-2354). A stalled builder is alive as far as its process shows: stop its agent first (TaskStop), then `sampo lane recover ID --agent-stopped`; recover never uses `--force`, and a lane it can't tell about (another host, no process record) is left alone with the reason. A usage-limit stop waits for the window's reset; a finished lane is `sampo lane merge ID`. A guard-refused merge names its grant instead; a conflict or red on a lane whose process is live counts only once old and quiet. A lane gets two recoveries: the third stop blocks its task with the cause and both attempts (`sampo task unblock` resets the count).
