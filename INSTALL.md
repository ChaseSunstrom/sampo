# Installing Sampo (for an agent doing it on someone's behalf)

If a person asked you to install Sampo, follow these steps in order. Ask before each change to their machine; don't
assume yes. The command it installs is `sampo`; `fm` still works as an alias for it.

1. **Check the basics.** Sampo needs Linux, macOS or WSL (not native Windows), `git` and Python 3.12.7 or newer as
   `python3`; Claude Code (`claude`) is optional. Run `uname -s`, `git --version`, `python3 --version` and
   `claude --version`: without Claude Code the installer skips its plugin and settings, puts `sampo` on PATH and wires
   Codex, Gemini CLI or opencode if they're installed.
   If Python is older or missing, the installer can install one (Homebrew on macOS, `uv` elsewhere, no sudo); say so.

2. **Read the installer before running it.** Fetch
   `https://raw.githubusercontent.com/ChaseSunstrom/sampo/main/install.sh` and read it. Tell the person, in plain
   words, what it will do by default:
   - clone Sampo to `~/.claude/foreman` and, with Claude Code, register it as a Claude Code plugin; without Claude
     Code, put `sampo` on PATH and run `sampo agents install` for each of Codex, Gemini CLI and opencode it finds;
   - install a set of third-party Claude Code plugins it works well with (skip with `--no-plugins`);
   - install the sampo-ui mod: a status band and dashboard pane (skip with `--no-mod`);
   - wire Sampo into `~/.claude`: a status line wrapper, deny rules for catastrophic commands, a marked block in
     `~/.claude/CLAUDE.md`, a rules file link, and a `~/.local/bin/fm` link (skip all of it with `--no-wiring`);
   - ask whether to make `bypassPermissions` the default permission mode. Sampo is built for bypass mode: its Ward
     blocks dangerous commands instead of Claude Code asking about every one. Use `--bypass` to turn it on without the
     question or `--no-bypass` to keep the current mode. Running non-interactively, the installer can't ask, so
     without `--bypass` the mode stays as it was.
   Every change is recorded so `~/.claude/foreman/plugin/uninstall.sh` can undo it.

3. **Ask which of those they want**, and build the command from the answers, for example
   `curl -fsSL https://raw.githubusercontent.com/ChaseSunstrom/sampo/main/install.sh | bash -s -- --no-plugins --bypass`.
   Run it only after they agree. The options (`install.sh --help` prints the same):

   | Flag | What it does |
   |---|---|
   | `--no-plugins` | skip the third-party plugin setup |
   | `--no-mod` | skip the sampo-ui mod (status band, dashboard pane, toasts) |
   | `--no-wiring` | leave `~/.claude` alone: no status line wrapper, deny rules, `CLAUDE.md` block, rules link or `sampo` link |
   | `--bypass` | make `bypassPermissions` the default permission mode without asking |
   | `--no-bypass` | don't set it and don't ask |
   | `--local-models` | pull the pinned Ollama embedding models (`bge-m3:567m`, `nomic-embed-text:v1.5`) for the moons funnel; needs Ollama installed (the installer never installs it) |
   | `--no-local-models` | don't pull and don't ask (on a terminal the installer asks; with no terminal it doesn't pull) |
   | `--desktop` | also install Sampo Desktop from its newest release |
   | `--build` | open Claude Code on the Sampo build (rebuild or resume from `BUILD_PROMPT.md`) |
   | anything else | passed through to `setup-plugins.sh` (for example `--security`, `--docs`, `--apply-conflicts`) |

   Environment: `FOREMAN_REPO` (git URL to clone), `FOREMAN_HOME` (where to put it, default `~/.claude/foreman`).

4. **Check it worked:** `~/.claude/foreman/plugin/bin/sampo doctor`. It lists only warnings and failures; "sampo doctor: OK"
   at the end means it's healthy. Fix or explain anything it reports.

5. **Hand over.** Tell them to restart their agent (in Claude Code, or run `/reload-plugins`), then give it any request
   in plain words, for example "fix the login timeout". `sampo demo` shows what Sampo does in about a minute, without using
   the model. `sampo --help` lists the everyday commands.

To remove it later: `~/.claude/foreman/plugin/uninstall.sh` (`--dry-run` previews; `state/` is kept unless
`--purge-state`).

**Upgrading an install from before the plugin rename.** The marketplace is now `sampo` and the plugins `sampo`,
`sampo-ui` and `sampo-design`: ids `sampo@sampo`, `sampo-ui@sampo` and `sampo-design@sampo`, commands `/sampo:next`,
agents `sampo:fm-builder`. Claude Code keeps a marketplace under the name it was added with, so an install made
before still has ids ending `@foreman` (the old plugin names, or `sampo` once the marketplace's renames map moved it).
It keeps working; to move it, run `sampo migrate plugins --dry-run` to see the change, then `sampo migrate plugins`
(install.sh does this for you). It re-adds the marketplace as `sampo` from the same folder and swaps the ids, with the
mod's options, in one write per file (`settings.json`, `plugins/installed_plugins.json`,
`plugins/known_marketplaces.json`), so the old and new ids are never enabled together; a copy of each file goes to
`~/.claude/sampo-migrate-plugins/`, and `sampo migrate plugins --undo` puts them back byte for byte. It moves a
marketplace read from a folder (install.sh's); one added from GitHub keeps its old ids and works, or is re-added by
hand (`/plugin marketplace add ChaseSunstrom/sampo`, `/plugin install sampo@sampo`, then uninstall the old id).
Sampo still reads the old ids and names wherever they were saved (settings, logs, notes); Claude Code names commands
after the plugin, so the old `/foreman:` commands are gone once a session starts on `sampo@sampo`: type `/sampo:`
instead. Restart open Claude Code sessions after the move (don't count on `/reload-plugins`): until then each keeps
the names it started with, and Sampo's notes to it use those. `sampo doctor` warns while an old id is enabled, and
when an old and a new one are enabled together, and names `sampo migrate plugins`.
