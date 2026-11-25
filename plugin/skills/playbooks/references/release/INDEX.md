<!-- Sampo's own index (written for Sampo, not ported). -->
# Release playbooks: showing what shipped

Paths are relative to the playbooks skill. The stage's other playbooks (release and changelog, dependency upgrades)
are listed in SKILL.md.

- `references/release/ship-to-main.md` — Push finished work to main: fetched main in a scratch worktree, oldest runtime, fast-forward only, every exit code read, CI-only failures.
- `references/release/demo-production.md` — Demos, release videos and GIFs: a script with beats, seeded fake data, recorded from code, captions, a scrub for secrets.
- `references/release/plugin-release.md` — Tagging a Claude Code plugin or marketplace entry: one version everywhere, strict validate, load it.
- `references/release/mobile-store-release.md` — Shipping to TestFlight or Play: gate, build number, archive, validate, internal track first.
- `references/release/go-release.md` — Releasing a Go module or GoReleaser binary: tidy modules, /v2 path, snapshot dry run.
- `references/release/release-and-changelog.md` — Cutting a version: gate the tagged commit, bump everywhere, user-facing changelog, migration notes.
- `references/release/dependency-admission.md` — Before adding a dependency: avoid it, name, licence, health, audit, weight, pin.
- `references/release/dependency-upgrade.md` — Bumping a library or toolchain: one per task, read the notes, baseline, fix forward, verify.
