---
name: release
description: Use whenever you release or ship: gate the commit, bump the version, changelog, tag, publish.
---

# Sampo: release

The procedure is `../playbooks/references/release/release-and-changelog.md`; this is how Sampo runs it. A release is a task like any other (`/sampo:intake` if none is active), and publishing is outward and hard to undo.

## Any project

`sampo release VERSION` runs this ritual in any repo as named steps with a checkpoint after each: clean tree, `sampo check`, bump the one manifest's version (pyproject.toml, package.json, Cargo.toml, a gemspec or VERSION), CHANGELOG's Unreleased headed VERSION, `sampo prove --release` (a red or unproven proof stops before the commit), commit, a local annotated tag. `--dry-run` prints the plan, `--resume` continues; `--push` pushes the branch and tag only with this repo's task's publish grant, and without one stops after the tag and names the push. By hand, the same steps:

1. **Clean tree, green gate** on the exact commit you'll tag: `sampo check` (no `--affected`), then `sampo check --python oldest` once when that Python is installed (the same before a push to main), nothing uncommitted. Red → fix first (`/sampo:debug-test`), never "fix after tagging".
2. **Version** by the project's scheme (semver: breaking → major, feature → minor, fix → patch); bump every place that carries it in one commit. A dependency bump in the same release → `../playbooks/references/release/dependency-upgrade.md`, one per task.
3. **Changelog:** move Unreleased under the new version with today's date; one user-facing line per change, task ids linked; breaking changes get a before → after migration note.
4. **Push, tag and publish** only with the task's publish grant (pushing the release commit is outward too): `sampo ask ID publish --why "<what goes where>"` asks the user; a standing yes they gave for releases is recorded with `sampo task set ID --allow publish`. No grant → stop at the local commit and tag, and say what's left.
5. **After the push:** `sampo ci-watch` follows the CI run (a failure becomes a FIX brief); install the published artifact from scratch and record that as evidence. A demo or release video → `../playbooks/references/release/demo-production.md`.

## Sampo's own repo

`sampo release VERSION` runs check, bump, changelog, commit, release-check, canary, push and round close as named steps with a checkpoint after each:
- `sampo release VERSION --dry-run` prints the plan first; `sampo release VERSION --resume` continues a stopped run; `--abandon` forgets one.
- `--ui V` is required when `mods/foreman-ui` changed since the last release.
- `sampo release-check` (sampo doctor and sampo demo in a temporary HOME) is one of the steps; run it alone to see a failure.
- `sampo release VERSION --push` merges into origin's main and pushes; it spends the active task's publish grant and refuses without one, after `--no-check`, or (unless `--stale-ok REASON`) on a red or stale guard replay. Without `--push` nothing leaves the machine; `--push` later runs only the push.

Never push to a remote or publish a package by hand to get around a refusal: report the refusal and what it needs.
