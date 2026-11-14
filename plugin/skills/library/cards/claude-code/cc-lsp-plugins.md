---
name: cc-lsp-plugins
title: LSP plugins
area: claude-code
tags: [claude-code, lsp, language-server, plugin, diagnostics]
detect: [file:.lsp.json]
related: [cc-plugins, cc-plugin-manifest, cc-plugin-debugging, cc-plugin-release]
---
A plugin can bundle language servers in `.lsp.json` (or `lspServers` in the manifest) so Claude gets diagnostics and code intelligence for those file types.

## Verify
- `python3 -m json.tool .lsp.json`: parses.
- `claude plugin validate .`: `lspServers` configs are strict objects, so an unknown key fails (path checks need 2.1.283 or later).
- `command -v <server-binary>`: the language server is installed and on PATH; Claude Code does not install it.
- `claude --plugin-dir ./plugin`: open a matching file and check `/plugin` for errors.

## Test first
Prove the server speaks LSP over stdio before wiring it.
```bash
body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{}}}'
printf 'Content-Length: %d\r\n\r\n%s' "${#body}" "$body" | timeout 5 gopls serve 2>/dev/null | head -c 200 | grep -q Content-Length \
  || echo "server did not answer initialize"
```
macOS has no `timeout`: use `gtimeout` (brew coreutils) there, or `python3 -c 'import subprocess,sys; subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]))' 5 gopls serve` beside it.
Write that first, then the config below.

## Do
- Minimal entry (keyed by name): `{"go": {"command": "gopls", "args": ["serve"], "extensionToLanguage": {".go": "go"}}}`.
- Required fields: `command` (a bare binary name; no spaces unless the path starts with `/`) and `extensionToLanguage` (keys start with a dot, at least one).
- Optional: `args`, `env`, `initializationOptions`, `settings`, `workspaceFolder`, `startupTimeout`, `requestTimeout` (default 60000 ms, 2.1.288 and later), `restartOnCrash` (default true), `maxRestarts`, `diagnostics` (default true).
- Put arguments in `args`, never inside `command`.
- Use `${CLAUDE_PLUGIN_ROOT}` for bundled binaries; it resolves in `command`, `args`, `env` and `workspaceFolder`.
- Document the install step for the server binary in the README.
- Keep `diagnostics` on unless the server is noisy; diagnostics are pushed into context after edits.

## Avoid
- A `command` with spaces and flags in one string: it fails. Split into `command` and `args`.
- Unknown keys (including a misspelled `extensionToLanguage`): the strict object rejects the plugin. Validate.
- `transport: "socket"` expecting a socket: Claude Code runs every server over stdio, so the server must not write logs to stdout. Send logs to stderr.
- Assuming the server is installed: a missing binary silently yields no intelligence. Check PATH and say so in docs.
- Extension keys without the dot (`go`): they do not match. Use `.go`.
- Servers that need a long warm-up with the default timeouts: raise `startupTimeout`.

## Review
- [ ] Every entry has `command` and a dotted `extensionToLanguage`.
- [ ] Arguments are in `args`.
- [ ] Server binary requirement is documented.
- [ ] The server logs to stderr only.
- [ ] Validate passes.
