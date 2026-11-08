#!/usr/bin/env bash
# Foreman: curated Claude Code plugin setup. Safe to re-run; never uninstalls anything.
#
# Usage: setup-plugins.sh [options]
#   --build-tools        also install plugin-dev (useful while building Foreman; disable afterward)
#   --security           also install the Trail of Bits security pack
#   --docs               also install Anthropic's document-skills (docx/xlsx/pptx/pdf)
#   --apply-conflicts    DISABLE plugins that compete with Foreman (default: only report them)
#   --dry-run            print what would happen, change nothing
#   -h, --help           show this help
#
# Rationale for every choice: BUILD_PROMPT.md, Appendix A.
set -uo pipefail

BUILD_TOOLS=0 SECURITY=0 DOCS=0 APPLY=0 DRY=0
for arg in "$@"; do
  case "$arg" in
    --build-tools) BUILD_TOOLS=1 ;;
    --security) SECURITY=1 ;;
    --docs) DOCS=1 ;;
    --apply-conflicts) APPLY=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "setup-plugins: unknown option '$arg' (try --help)" >&2; exit 2 ;;
  esac
done

command -v claude >/dev/null 2>&1 || { echo "setup-plugins: 'claude' not found on PATH" >&2; exit 1; }
# Inside a Claude Code session (its shells set CLAUDECODE=1) Foreman's guard can't see the installs below, so they'd
# skip the user's yes: there, install one plugin at a time with `fm ask ID plugin --pin <id>`, then `fm plugins install`.
if [ "${CLAUDECODE:-}" = 1 ] && [ "$DRY" != 1 ]; then
  echo "setup-plugins: not inside a Claude Code session (the installs would skip the user's yes). Run it from your own" \
       "terminal, or install one plugin: fm ask <task> plugin --pin <id>, then fm plugins install <id>." >&2
  exit 3
fi

OFFICIAL=claude-plugins-official
run() { if [ "$DRY" = 1 ]; then echo "    (dry-run) $*"; else "$@"; fi; }
say() { printf '%s\n' "$*"; }

# ---------- read current plugin state once (JSON; python3 -> jq -> text fallback) ----------
STATE_JSON="$(claude plugin list --json 2>/dev/null || echo '[]')"
list_ids() { # prints "<id> <enabled:true|false>" per installed plugin
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys
for p in json.load(sys.stdin): print(p["id"], str(p.get("enabled", True)).lower())' <<<"$STATE_JSON"
  elif command -v jq >/dev/null 2>&1; then
    jq -r '.[] | "\(.id) \(.enabled // true)"' <<<"$STATE_JSON"
  else
    claude plugin list 2>/dev/null | awk '/^ *> /{id=$2} /Status:/{print id, ($0 ~ /enabled|loaded/) ? "true" : "false"}'
  fi
}
IDS="$(list_ids)"
has()     { grep -q "^$1 " <<<"$IDS"; }
enabled() { grep -q "^$1 true$" <<<"$IDS"; }

add_marketplace() { run claude plugin marketplace add "$1" >/dev/null 2>&1 || say "  ! could not add marketplace $1"; }

install() { # install <plugin@marketplace> [required-binary]
  local id="$1" bin="${2:-}"
  if has "$id"; then
    if [ -n "$bin" ] && ! command -v "$bin" >/dev/null 2>&1; then
      say "  ⚠ $id is installed but '$bin' is not on PATH, so it will error. Install $bin or disable the plugin."
    elif ! enabled "$id"; then
      if run claude plugin enable "$id" >/dev/null 2>&1; then say "  ↺ $id (re-enabled)"; else say "  ! $id is disabled and couldn't be re-enabled"; fi
    else
      say "  ✓ $id"
    fi
    return
  fi
  if [ -n "$bin" ] && ! command -v "$bin" >/dev/null 2>&1; then
    say "  · $id skipped ('$bin' not on PATH)"; return
  fi
  if run claude plugin install "$id" --scope user >/dev/null 2>&1; then say "  + $id"; else say "  ! $id failed: check the name in /plugin > Discover"; fi
}

conflict() { # conflict <plugin@marketplace> <reason>
  enabled "$1" || return 0
  if [ "$APPLY" = 1 ]; then
    run claude plugin disable "$1" >/dev/null 2>&1 && say "  - disabled $1 ($2)" || say "  ! could not disable $1"
  else
    say "  ⚠ $1 is enabled: $2"
    say "      disable with: claude plugin disable $1   (or re-run with --apply-conflicts)"
  fi
}

# ---------- marketplaces ----------
say "Marketplaces"
add_marketplace anthropics/claude-plugins-official
[ "$SECURITY" = 1 ] && add_marketplace trailofbits/skills
[ "$DOCS" = 1 ] && add_marketplace anthropics/skills
say "  ✓ done"

# ---------- core ----------
say "Core"
install "context7@$OFFICIAL"              # version-correct library docs (grounding)
install "security-guidance@$OFFICIAL"     # edit-time security warnings + review (SECURITY phase)
install "code-review@$OFFICIAL"           # read-only review agents (L-tier final review only)
install "skill-creator@$OFFICIAL"         # author and evaluate skills
install "claude-md-management@$OFFICIAL"  # CLAUDE.md audits (Foreman picks one owner for CLAUDE.md lint)
[ "$BUILD_TOOLS" = 1 ] && install "plugin-dev@$OFFICIAL"   # plugin/hook authoring toolkit (~1.7k always-on tokens: disable after the build)

# ---------- language intelligence: only for language servers already on PATH ----------
say "Language servers (installed only when the server binary exists)"
install "clangd-lsp@$OFFICIAL"        clangd
install "pyright-lsp@$OFFICIAL"       pyright-langserver
install "typescript-lsp@$OFFICIAL"    typescript-language-server
install "gopls-lsp@$OFFICIAL"         gopls
install "rust-analyzer-lsp@$OFFICIAL" rust-analyzer

# ---------- optional packs ----------
if [ "$SECURITY" = 1 ]; then
  say "Security pack (Trail of Bits)"
  for p in differential-review insecure-defaults static-analysis sharp-edges supply-chain-risk-auditor c-review; do
    install "$p@trailofbits"
  done
fi
if [ "$DOCS" = 1 ]; then
  say "Document skills"
  install "document-skills@anthropic-agent-skills"
fi

# ---------- conflicts with Foreman ----------
# Same plugins as KNOWN_CONFLICTS in plugin/lib/fmplugins.py (`fm plugins check`); tests/test_docs.py keeps them equal.
say "Conflicts with Foreman"
conflict "ecc@ecc"                         "~41k always-on tokens (measured Sept 2026) and 24 hook handlers (incl. sync hooks on every tool call); its own memory, learning, and planning system. Foreman ports its useful procedures as /sampo:playbooks"
conflict "superpowers@$OFFICIAL"           "a second orchestrator (brainstorm -> plan -> execute); Foreman ports its debugging, TDD and verification procedures"
conflict "feature-dev@$OFFICIAL"           "duplicates Foreman's intake and planning"
conflict "ralph-loop@$OFFICIAL"            "keeps Claude running via a Stop hook; collides with Foreman's completion gate"
conflict "example-skills@anthropic-agent-skills" "12 mostly unrelated skills; duplicates skill-creator and frontend-design"
say "  ✓ checked"

# ---------- advisories ----------
say "Advisories"
command -v node >/dev/null 2>&1 || say "  ⚠ node is not on this non-interactive PATH; ponytail/claude-hud hooks need it (common on nvm/Nix setups)"
for id in "playwright@$OFFICIAL" "chrome-devtools-mcp@$OFFICIAL"; do
  enabled "$id" && say "  · $id is enabled globally; consider project/local scope for web projects only"
done
for id in design@synced figma@synced; do
  enabled "$id" && say "  · $id is synced from claude.ai; turn it off here if you're not doing design work (it can't be uninstalled locally)"
done
say "  · measure any plugin's context cost with: claude plugin details <plugin@marketplace>"

[ "$DRY" = 1 ] && say "Dry run: nothing changed." || say "Done. Restart Claude Code (or /reload-plugins) to load changes."
