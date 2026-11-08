#!/usr/bin/env bash
# Sampo bootstrap. Public repo:
#   curl -fsSL https://raw.githubusercontent.com/ChaseSunstrom/sampo/main/install.sh | bash
# Private repo (uses your gh login):
#   gh repo clone ChaseSunstrom/sampo ~/.claude/foreman && ~/.claude/foreman/install.sh
#
# Options:
#   --build          open Claude Code on the Sampo build (rebuild or resume from BUILD_PROMPT.md)
#   --desktop        also install Foreman Desktop from its newest release (Linux x86_64 AppImage, macOS app)
#   --no-plugins     skip setup-plugins.sh
#   --bypass         make bypassPermissions the default permission mode without asking
#   --no-bypass      don't set it, and don't ask (on a terminal the installer asks; with no terminal it doesn't set it)
#   --local-models   pull the pinned Ollama embedding models (bge-m3, nomic-embed-text) for the moons funnel; needs ollama installed
#   --no-local-models  don't, and don't ask (on a terminal the installer asks; with no terminal it doesn't pull)
#   --no-mod         don't install the foreman-ui mod (band above the prompt, dashboard pane, toasts)
#   --no-wiring      don't wire Sampo into ~/.claude (statusLine wrapper, deny rules, CLAUDE.md block, rules symlink)
#   anything else    passed through to setup-plugins.sh (e.g. --security, --docs, --apply-conflicts)
# Environment:
#   FOREMAN_REPO     git URL to clone (default below)
#   FOREMAN_HOME     where to put it (default ~/.claude/foreman; BUILD_PROMPT.md assumes this path)
#   FOREMAN_DESKTOP_REPO  GitHub owner/repo the desktop app's releases come from (default below)
set -euo pipefail

FOREMAN_REPO="${FOREMAN_REPO:-https://github.com/ChaseSunstrom/sampo.git}"
FOREMAN_HOME="${FOREMAN_HOME:-$HOME/.claude/foreman}"
FOREMAN_DESKTOP_REPO="${FOREMAN_DESKTOP_REPO:-ChaseSunstrom/foreman-desktop}"

EMBED_PINS="bge-m3:567m nomic-embed-text:v1.5"   # T-2880: exact tags (doctor.EMBED_PINS is the same list)

BUILD=0 PLUGINS=1 BYPASS=ask LOCAL=ask WIRING=1 MOD=1 DESKTOP=0 PASS=()
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=1 ;;
    --desktop) DESKTOP=1 ;;
    --no-plugins) PLUGINS=0 ;;
    --bypass) BYPASS=1 ;;
    --no-bypass) BYPASS=0 ;;
    --local-models) LOCAL=1 ;;
    --no-local-models) LOCAL=0 ;;
    --no-mod) MOD=0 ;;
    --no-wiring) WIRING=0 ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) PASS+=("$arg") ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'sampo: %s\n' "$*" >&2; exit 1; }
command -v git >/dev/null 2>&1 || die "'git' is required but not on PATH"
# Claude Code is one surface, not a requirement (T-2174): without it the CC-only steps (plugins, the plugin itself,
# the permission mode, ~/.claude wiring) are skipped and Sampo goes into whichever other agents are here.
CLAUDE=1; command -v claude >/dev/null 2>&1 || CLAUDE=0
if [ "$CLAUDE" = 0 ]; then
  [ "$BUILD" = 1 ] && die "--build runs Claude Code on BUILD_PROMPT.md; install Claude Code first, or drop --build"
  [ "$PLUGINS" = 1 ] && [ ${#PASS[@]} -gt 0 ] && echo "sampo: no Claude Code, so its plugin flags are ignored: ${PASS[*]}"
  PLUGINS=0 BYPASS=0
fi

# 0. Python: fm and every hook run on the python3 on PATH. Sampo needs 3.12.7+: older ones break `communicate()`
#    on a closed stdin, and argparse before 3.12.7 (and in 3.13.0) drops `fm task evidence ID --ac N CMD RESULT`.
py_ok() { python3 -c 'import sys; v = sys.version_info[:3]; sys.exit(not (v >= (3, 12, 7) and v != (3, 13, 0)))' >/dev/null 2>&1; }
if ! py_ok; then
  case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) die "Sampo needs Linux or macOS; on Windows, install it inside WSL" ;; esac
  have="$(python3 --version 2>&1)" || have="no python3"
  if [ "$(uname -s)" = Darwin ] && command -v brew >/dev/null 2>&1; then
    say "Installing Python with Homebrew (found: $have; Sampo needs 3.12.7+)"
    brew install python
  else
    if ! command -v uv >/dev/null 2>&1; then
      command -v curl >/dev/null 2>&1 || die "found $have; Sampo needs Python 3.12.7+ (install it, or curl so uv can)"
      say "Installing uv to install Python (no sudo needed)"
      curl -LsSf https://astral.sh/uv/install.sh | sh
      export PATH="$HOME/.local/bin:$PATH"
    fi
    say "Installing Python 3.13 with uv (found: $have; Sampo needs 3.12.7+)"
    uv python install 3.13 --default
    uv python update-shell   # later shells, and so the hooks, find it too
    export PATH="$(uv python dir --bin):$PATH"
  fi
  hash -r
  py_ok || die "python3 on PATH is still $(python3 --version 2>&1); put the new one's folder ahead of it on PATH and rerun"
fi

# 1. Get or update the repo
if [ -d "$FOREMAN_HOME/.git" ]; then
  say "Updating $FOREMAN_HOME"
  if [ -z "$(git -C "$FOREMAN_HOME" status --porcelain --untracked-files=no)" ] &&
     [ "$(git -C "$FOREMAN_HOME" rev-parse --abbrev-ref HEAD)" = main ]; then
    git -C "$FOREMAN_HOME" pull --ff-only --quiet || say "Couldn't fast-forward; leaving the local copy as is"
  else
    say "Local changes or a non-main branch checked out (e.g. a Sampo build in progress); not pulling"
  fi
elif [ -e "$FOREMAN_HOME" ]; then
  die "$FOREMAN_HOME exists but isn't a git repo; move it aside first"
else
  placeholder="YOUR_GITHUB""_USER"   # split so configure-repo.sh doesn't rewrite this check
  case "$FOREMAN_REPO" in
    *"$placeholder"*) die "set FOREMAN_REPO=<git url>, or run ./configure-repo.sh <github-user> before pushing" ;;
  esac
  say "Cloning $FOREMAN_REPO -> $FOREMAN_HOME"
  mkdir -p "$(dirname "$FOREMAN_HOME")"
  git clone --quiet "$FOREMAN_REPO" "$FOREMAN_HOME"
fi
mkdir -p "$FOREMAN_HOME/state" "$FOREMAN_HOME/local" "$FOREMAN_HOME/backups"
chmod +x "$FOREMAN_HOME"/*.sh

# 2. Curated plugins
if [ "$PLUGINS" = 1 ]; then
  say "Setting up plugins"
  [ "$BUILD" = 1 ] && PASS+=(--build-tools)   # plugin-dev: only needed while building Sampo itself
  "$FOREMAN_HOME/setup-plugins.sh" ${PASS[@]+"${PASS[@]}"}
fi

# 3. The Sampo plugin itself, from this repo as a local marketplace (loads in place: edits apply on /reload-plugins)
if [ "$CLAUDE" = 1 ]; then
say "Installing the Sampo plugin"
# T-3707: the marketplace is named sampo. An install made before (ids ending @foreman: the plugin's old name, or sampo
# once the renames map ran) moves with sampo migrate plugins, one write per file, so the old and new ids never load
# together; installing sampo@sampo beside it would load every hook twice
had() { grep -qE "\"($1)@foreman\"" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/installed_plugins.json" 2>/dev/null; }
OLD_MOD=0; had 'foreman-ui|sampo-ui' && OLD_MOD=1  # read before the move: the mod moves (or stays) with the plugin
if had 'foreman|sampo|foreman-ui|sampo-ui'; then
  if python3 "$FOREMAN_HOME/plugin/bin/sampo" migrate plugins >/dev/null 2>&1; then
    say "  moved the plugin to sampo@sampo (sampo migrate plugins --undo puts it back; restart open sessions)"
  else
    say "  ! the plugin keeps its old id: sampo migrate plugins --dry-run says why"
  fi
else
  claude plugin marketplace add "$FOREMAN_HOME" >/dev/null 2>&1 || true
  claude plugin install sampo@sampo --scope user >/dev/null 2>&1 || die "couldn't install sampo@sampo (run: claude plugin validate $FOREMAN_HOME)"
fi
# The UI mod (band, pane, toasts) is optional: builds without function-hook plugins keep the statusline and fm watch
if [ "$MOD" = 1 ]; then
  if [ "$OLD_MOD" = 1 ]; then
    :  # moved with the plugin above, or kept under its old id with it
  elif claude plugin install sampo-ui@sampo --scope user >/dev/null 2>&1; then
    say "  installed sampo-ui (band, /fm pane, toasts; --no-mod skips it, claude plugin disable sampo-ui@sampo turns it off)"
  else
    say "  (sampo-ui not installed: this Claude Code build has no mods; the statusline and fm watch still work)"
  fi
fi
fi

# 4. Permission mode: bypass is Sampo's recommended mode (its guard hook + deny rules are the brakes; BUILD_PROMPT.md
# §4.7), but it's the person's choice (T-0871): asked on a terminal, set by --bypass, otherwise left as it was.
if [ "$BYPASS" = ask ]; then
  BYPASS=0
  if (exec </dev/tty) 2>/dev/null; then
    printf '\nSampo works best with Claude Code in bypassPermissions mode: Claude runs commands without asking each\n'
    printf 'time, and Sampo'"'"'s guard blocks the dangerous ones (rm -rf ~, force-push to main, secrets...) instead.\n'
    printf 'Make bypassPermissions your default mode in ~/.claude/settings.json? (undo: plugin/uninstall.sh) [y/N] '
    read -r ans </dev/tty || ans=""
    case "$ans" in y|Y|yes|YES) BYPASS=1 ;; esac
  fi
  [ "$BYPASS" = 1 ] || say "Permission mode left as it was (Sampo's guard still runs; rerun with --bypass to turn bypass on)"
fi
if [ "$BYPASS" = 1 ]; then
  settings="$HOME/.claude/settings.json"
  mkdir -p "$HOME/.claude"
  [ -f "$settings" ] && cp "$settings" "$FOREMAN_HOME/backups/settings.json.$(date +%Y%m%d-%H%M%S)"
  if command -v python3 >/dev/null 2>&1 && python3 - "$settings" "$FOREMAN_HOME/state/install-manifest.json" <<'PY'
import json, os, sys
path, manifest = sys.argv[1], sys.argv[2]
data = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
# Record the mode we found before the first change, so uninstall can restore it.
m = json.load(open(manifest)) if os.path.exists(manifest) else {}
if "defaultMode_original" not in m:
    m["defaultMode_original"] = (data.get("permissions") or {}).get("defaultMode")
    # What existed before Sampo touched anything, so uninstall can remove what we created (fm install-user keeps these).
    m.setdefault("settings_created", not data)
    m.setdefault("permissions_created", "permissions" not in data)
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    with open(manifest + ".tmp", "w") as f:
        json.dump(m, f, indent=2, sort_keys=True)
    os.replace(manifest + ".tmp", manifest)
data.setdefault("permissions", {})["defaultMode"] = "bypassPermissions"
tmp = path + ".foreman-tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
PY
  then
    say "Default permission mode: bypassPermissions (Claude Code asks you to confirm once on next launch)"
  else
    say "Couldn't update $settings; add  \"permissions\": {\"defaultMode\": \"bypassPermissions\"}  yourself"
  fi
fi

# 5. Sampo user wiring (reversible: plugin/uninstall.sh or `fm uninstall-user`)
if [ "$WIRING" = 1 ] && [ "$CLAUDE" = 1 ]; then
  say "Wiring Sampo into ~/.claude (statusLine wrapper, deny rules, CLAUDE.md block, rules symlink)"
  python3 "$FOREMAN_HOME/plugin/bin/fm" install-user || say "Wiring failed; run: $FOREMAN_HOME/plugin/bin/fm install-user --dry-run"
elif [ "$WIRING" = 1 ]; then
  say "No Claude Code here: putting fm on PATH and wiring the agents found"
  # setup.ensure_fm_link: makes the link, repairs a dangling one of Sampo's, leaves anyone else's fm alone
  python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from sampo import setup; setup.ensure_fm_link()' "$FOREMAN_HOME/plugin/lib" || true
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "Add ~/.local/bin to your PATH so any shell finds fm" ;; esac
  found=0
  for agent in codex gemini opencode; do
    command -v "$agent" >/dev/null 2>&1 || continue
    found=1
    python3 "$FOREMAN_HOME/plugin/bin/fm" agents install "$agent" || say "Couldn't wire $agent; run: fm agents install $agent"
  done
  [ "$found" = 1 ] || say "No coding agent found: fm code runs Sampo's loop on any model (fm code --help); fm agents install codex|gemini|opencode wires one later"
fi

# 6. Foreman Desktop (--desktop): the newest release's build for this machine, no Rust or bun needed here
if [ "$DESKTOP" = 1 ]; then
  asset() {  # the download URL of the newest release's asset whose name ends with $1
    curl -fsSL "https://api.github.com/repos/$FOREMAN_DESKTOP_REPO/releases/latest" | python3 -c '
import json, sys
print(next(a["browser_download_url"] for a in json.load(sys.stdin)["assets"] if a["name"].endswith(sys.argv[1])))' "$1" \
      || die "the newest Foreman Desktop release ($FOREMAN_DESKTOP_REPO) has no *$1"
  }
  case "$(uname -s)/$(uname -m)" in
    Linux/x86_64)
      url="$(asset _amd64.AppImage)"
      mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
      curl -fsSL "$url" -o "$HOME/.local/bin/foreman-desktop.part"
      chmod +x "$HOME/.local/bin/foreman-desktop.part"
      mv "$HOME/.local/bin/foreman-desktop.part" "$HOME/.local/bin/foreman-desktop"
      printf '[Desktop Entry]\nType=Application\nName=Foreman\nComment=Every project, session and agent on every device\nExec=%s\nTerminal=false\nCategories=Development;\n' \
        "$HOME/.local/bin/foreman-desktop" > "$HOME/.local/share/applications/foreman-desktop.desktop"
      say "Foreman Desktop installed: $HOME/.local/bin/foreman-desktop (and in your app menu)" ;;
    Darwin/*)
      url="$(asset .dmg)"
      dmg="$(mktemp -d)/foreman.dmg" mnt="$(mktemp -d)"
      curl -fsSL "$url" -o "$dmg"
      hdiutil attach -nobrowse -quiet -mountpoint "$mnt" "$dmg"
      mkdir -p "$HOME/Applications"
      rm -rf "$HOME/Applications/Foreman.app"
      cp -R "$mnt/Foreman.app" "$HOME/Applications/"
      hdiutil detach -quiet "$mnt"
      say "Foreman Desktop installed: $HOME/Applications/Foreman.app" ;;
    *) die "Foreman Desktop has builds for Linux x86_64 and macOS; build it from https://github.com/$FOREMAN_DESKTOP_REPO" ;;
  esac
fi

# 6. Local embedding models (T-2880): opt-in like bypass. Sampo finds Ollama's embedders by itself (no profile to
# write); this only pulls the pinned ones. It never installs the runtime: that needs root and is Ollama's to ship.
if [ "$LOCAL" = ask ]; then
  LOCAL=0
  if (exec </dev/tty) 2>/dev/null; then
    printf '\nPull the local embedding models Sampo'"'"'s moons funnel uses (%s; ~1 GB, needs Ollama)? [y/N] ' "$EMBED_PINS"
    read -r ans </dev/tty || ans=""
    case "$ans" in y|Y|yes|YES) LOCAL=1 ;; esac
  fi
fi
if [ "$LOCAL" = 1 ]; then
  if command -v ollama >/dev/null 2>&1; then
    for m in $EMBED_PINS; do
      say "Pulling $m"
      ollama pull "$m" || say "Couldn't pull $m (is the Ollama server running? retry: ollama pull $m)"
    done
  else
    say "Ollama isn't installed: get it from https://ollama.com/download, then rerun with --local-models (or ollama pull each of: $EMBED_PINS)"
  fi
fi

say "Sampo is installed at $FOREMAN_HOME"
if [ "$BUILD" = 1 ] && (: </dev/tty) 2>/dev/null; then   # a terminal we can actually open (not just a device node)
  say "Starting the build (it pauses once for plan approval unless BUILD_GATE is off)"
  cd "$FOREMAN_HOME"
  exec claude "Run the Sampo build: read $FOREMAN_HOME/BUILD_PROMPT.md in full and execute it. If $FOREMAN_HOME/local/PLAN.md exists, resume from it instead of starting over." </dev/tty
fi
if [ "$CLAUDE" = 1 ]; then
  printf '\nNext: start Claude Code in any repo and give it a request (e.g. FIX: ...).\n      /sampo:status shows where things stand; %s/MASTER.md explains everything.\n' "$FOREMAN_HOME"
else
  printf '\nNext: start your agent in any repo and give it a request (e.g. FIX: ...), or run fm code "<request>".\n      fm status shows where things stand; %s/MASTER.md explains everything.\n' "$FOREMAN_HOME"
fi
