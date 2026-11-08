# Sampo (plugin)

The Sampo plugin (installed as `sampo@sampo`; an install from before the rename has ids ending `@foreman`, which `sampo migrate plugins` moves): hooks, the `fm` state CLI, skills, read-only agents and the visibility layer. It loads in place from this repo through the local `sampo` marketplace, so edits apply on `/reload-plugins`.

- **Hooks** (`hooks/hooks.json` → `hooks/hook <Event>`): SessionStart / UserPromptSubmit inject short factual state; PreToolUse runs the guard (fails closed) and scope notes; PostToolUse(+Failure) and subagent events feed the ledger and `fm watch`; PreCompact checkpoints; Stop runs the evidence gate and drive mode; MessageDisplay adds the reply badge; Notification and Stop emit terminal titles and notifications; SessionEnd records activity.
- **CLI** `bin/fm` (Python 3 stdlib): the only writer of `~/.claude/foreman/state/`. `fm --help`. Consent: `fm ask` (raises Claude Code's permission prompt; only the user's approval there grants it); audits: `fm task audit`; autonomy: `fm autonomy standard|full`; tool-less brainstorm children: `fm ideas`; headless: `fm serve` (Remote Control under systemd) and `fm run` (fresh session per task).
- **Skills**: `/sampo:intake`, `brainstorm`, `next`, `resume`, `reflect`, `playbooks`, `debug-test`, `release`, `research`, `directions`, `ui-design`, `status`, `improve` (Claude starts these itself when you ask in plain words) and `capture`, `tidy`, `doctor` and `design-extract` (you only), plus `/sampo:build`.
- **Agents**: `sampo:fm-recon`, `sampo:fm-reviewer` (read-only; the reviewer runs one audit lens per brief).
- **Protected core**: everything in `lib/`, `bin/`, `hooks/`, `evals/`, `native/` and `rules/foreman.md`; edits need the user's yes to `fm ask ID core`. So does anything that could swap or poison `native/sampo-fast`, the binary `hooks/fast` runs (T-3607):
  - cargo, rustc or rustup output aimed into `plugin/` from any folder: `--target-dir`, `--artifact-dir`, `--root`, `-o`, `--emit`, `CARGO_TARGET_DIR`, `CARGO_BUILD_ARTIFACT_DIR`, `CARGO_BUILD_BUILD_DIR`, `CARGO_INSTALL_ROOT`, `-o` in `RUSTFLAGS`, any path in an inline `--config KEY=VALUE`;
  - a cargo build of the main checkout's `native/` with build settings: `RUSTC*`, `RUSTFLAGS`, `RUSTDOC*`, `RUSTUP_*` (`RUSTUP_TOOLCHAIN`), `PATH`, `--config`, a toolchain given as a path (`cargo +/x`, `rustup run /x`), and every other `CARGO_*` but `CARGO_TERM_*`, `CARGO_NET_*`, `CARGO_INCREMENTAL` and `CARGO_PROFILE_*` (except a profile's `RUSTFLAGS` and `PANIC`), which pass;
  - `.cargo/config(.toml)` or `rust-toolchain(.toml)` at or above `native/`, cargo home's config and `bin/`, rustup's `toolchains/` and `settings.toml` (`rustup toolchain link`, `rustup default /path`), and a `.cargo/config` anywhere (a lane's) with a path that lands in the main checkout, read as cargo reads it (from the config's folder or the one above `.cargo`, `~` and `$HOME` expanded, normalized, links followed);
  - `cargo --config FILE` and rustc `@FILE` arguments (on rustc's command line, in `RUSTFLAGS`, an inline `--config` or a `.cargo/config`) are refused outright: the guard can't read them.

  A standing core yes never covers the binary, its `.sha256`, `target/`, or any `.cargo` or `rust-toolchain` file.
- **Rules**: `rules/foreman.md`, symlinked into `~/.claude/rules/` by `fm install-user`.

Tests: `python3 -m unittest discover -s tests -t tests` · hook latency: `python3 tests/bench_hooks.py` · live scenarios: `python3 tests/e2e/scenarios.py` · install round trip: `python3 tests/e2e/roundtrip.py` · evals: `evals/README.md`.

Full system map: `../MASTER.md`. Remove: `./uninstall.sh`. Ported material and licenses: `THIRD_PARTY_LICENSES.md`.
