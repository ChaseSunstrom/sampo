// T-3701: a synthetic `fm ui --json` view with every Board panel's facts (no real project's data): lanes building,
// waiting to merge and merged, sessions, history, checks, the Sampo sessions' CPU. The render-cost test, the board
// tests and the QA images draw it. Plain JSON after the export, so tools read it as data too; keep it that way.
export default {
  "v": 1,
  "project": "demo-1a2b3c",
  "root": "/repo",
  "mode": {"autonomy": "full", "drive": true, "sensitive": false},
  "active": {
    "id": "T-0007", "type": "FIX", "tier": "M", "title": "Login times out on slow links", "status": "active", "stage": "verifying",
    "words": "the login keeps timing out for people on slow links: make it retry once and tell them it did, without hiding real outages",
    "stages": ["planning", "ready", "executing", "verifying", "auditing", "closing"],
    "steps": [
      {"n": 1, "text": "red test: a 6 s link logs in", "done": true, "current": false},
      {"n": 2, "text": "raise the timeout to 20 s", "done": true, "current": false},
      {"n": 3, "text": "retry once on a slow link", "done": false, "current": true}
    ],
    "criteria": [{"n": 1, "text": "slow wifi logs in", "verify": "pytest -q tests/test_login.py", "checked": true},
                 {"n": 2, "text": "a dead link fails in 30 s", "verify": "pytest -q tests/test_login.py", "checked": false}],
    "audits": {"done": 1, "need": 2},
    "blockers": [],
    "on_task_s": 1500
  },
  "next": "T-0007 step 3/3: retry once on a slow link",
  "lanes": {
    "building": [
      {"id": "T-0021", "type": "FEATURE", "tier": "M", "title": "Live page chart legend", "steps_done": 2, "steps_total": 5, "files": ["docs/board.md", "plugin/web/live/board.css", "plugin/web/live/board.js", "plugin/web/live/charts.js", "plugin/web/live/index.html", "plugin/web/live/live.css", "plugin/web/live/live.js"]},
      {"id": "T-0022", "type": "FIX", "tier": "S", "title": "Retry the upload", "steps_done": 1, "steps_total": 3, "files": ["plugin/lib/sampo/upload.py", "plugin/tests/test_upload.py"]}
    ],
    "merging": [
      {"id": "T-0023", "type": "CLEAN", "tier": "S", "title": "Tidy the date helpers", "steps_done": 3, "steps_total": 3, "files": ["plugin/lib/sampo/dates.py", "plugin/tests/test_dates.py"]},
      {"id": "T-0024", "type": "PERFORMANCE", "tier": "M", "title": "Cache the view build", "steps_done": 4, "steps_total": 4, "files": ["plugin/lib/sampo/live.py", "plugin/lib/sampo/watch.py", "plugin/web/live/live.js"]}
    ],
    "closing": [
      {"id": "T-0019", "type": "FIX", "tier": "S", "title": "Flaky clock test", "steps_done": 2, "steps_total": 2}
    ]
  },
  "queue": [
    {"id": "T-0008", "type": "CLEAN", "tier": "S", "title": "Tidy helpers", "status": "planned", "steps_done": 1, "steps_total": 3},
    {"id": "T-0010", "type": "SECURITY", "tier": "M", "title": "Redact tokens in the log", "status": "planned", "steps_done": 0, "steps_total": 4},
    {"id": "T-0009", "type": "FEATURE", "tier": "L", "title": "CSV export", "status": "planned", "waits": "plan approval",
     "plan": {"interpretation": "export the report table", "approach": "csv module vs pandas: csv module",
              "steps": [{"n": 1, "text": "write the exporter", "done": false, "current": true}],
              "criteria": [{"n": 1, "text": "opens in a spreadsheet", "verify": "pytest -q", "checked": false}]}}
  ],
  "queue_total": 12,
  "inbox": [{"id": "T-0011", "type": "CLEAN", "tier": "S", "title": "Merge date helpers", "status": "captured", "age_days": 2, "source": "user"},
            {"id": "T-0012", "type": "RESEARCH", "tier": "S", "title": "Which chart lib", "status": "captured", "age_days": 9}],
  "inbox_total": 5,
  "inbox_yours": 1,
  "approvals": [],
  "closed": [{"id": "T-0006", "status": "done", "title": "Fix the flaky clock test", "grade": "ok", "lenses": ["edge"]}],
  "recent": [],
  "history": [
    {"time": "09:12", "mark": "⚑", "task": "T-0011", "text": "captured", "title": "Merge date helpers"},
    {"time": "09:40", "mark": "✓", "task": "T-0006", "text": "done", "title": "Fix the flaky clock test"},
    {"time": "10:05", "mark": "✗", "task": "T-0007", "text": "step 1 failed", "title": "Login times out on slow links"},
    {"time": "10:09", "mark": "✓", "task": "T-0007", "text": "step 1 passed", "title": "Login times out on slow links"},
    {"time": "10:31", "mark": "◆", "task": "T-0021", "text": "lane started", "title": "Live page chart legend"},
    {"time": "10:48", "mark": "✓", "task": "T-0007", "text": "step 2 passed", "title": "Login times out on slow links"},
    {"time": "11:02", "mark": "✓", "task": "T-0024", "text": "lane finished", "title": "Cache the view build"},
    {"time": "11:20", "mark": "✓", "task": "T-0019", "text": "merged", "title": "Flaky clock test"}
  ],
  "fleet": [
    {"session": "a1b2c3d4e5f6", "project": "demo-1a2b3c", "task": "T-0007", "title": "Login times out on slow links", "context_pct": 41,
      "model": "Opus 5.5", "seen": "2026-01-02T10:00:00Z"},
    {"session": "b7c8d9e0f1a2", "project": "demo-1a2b3c", "task": "T-0021", "title": "Live page chart legend", "context_pct": 63, "mail": 1,
      "model": "Sonnet 5", "seen": "2026-01-02T09:59:30Z"},
    {"session": "c3d4e5f6a7b8", "project": "shop-9f8e7d", "task": null, "title": null, "context_pct": 12, "model": "Opus 5.5",
      "seen": "2026-01-02T09:40:00Z"},
    {"session": "d4e5f6a7b8c9", "project": "notes-4c5d6e", "task": "T-0102", "title": "Sync the inbox", "context_pct": 86,
      "model": "Haiku 4.5", "seen": "2026-01-02T09:59:50Z"},
    {"session": "e5f6a7b8c9d0", "project": "shop-9f8e7d", "task": "T-0311", "title": "Checkout totals", "context_pct": 27,
      "model": "Sonnet 5", "seen": "2026-01-02T09:58:40Z"}
  ],
  "worktrees": [
    {"path": "/repo/.claude/worktrees/agent-0a1b", "branch": "worktree-agent-0a1b", "task": "T-0021", "agent": "0a1b", "age_s": 1300, "ahead": 2, "open": true},
    {"path": "/repo/.claude/worktrees/agent-2c3d", "branch": "worktree-agent-2c3d", "task": "T-0022", "agent": "2c3d", "age_s": 600, "ahead": 0, "open": true}
  ],
  "health": {"hook_p95_ms": 31, "guard_blocks": 0, "hook_errors": 0},
  "watch": [],
  "latency": [20, 25, 31, 22, 40, 28],
  "checks": {"at": "2026-10-02T19:40:00Z", "results": [
    {"cmd": "python3 -m unittest -q", "exit": 0, "s": 140.2},
    {"cmd": "claude plugin test mods/foreman-ui", "exit": 0, "s": 66.0},
    {"cmd": "ruff check", "exit": 0, "s": 1.2},
    {"cmd": "tsc -p mods/foreman-ui", "exit": 0, "s": 9.8},
    {"cmd": "docs drift", "exit": 0, "s": 3.1},
    {"cmd": "bench", "exit": 1, "s": 11.0, "note": "slower than usual"}
  ]},
  "train": {"running": true, "landed": ["T-0015", "T-0016", "T-0017", "T-0019"], "landed_at": ["2026-01-02T07:12:00Z", "2026-01-02T08:05:00Z", "2026-01-02T09:02:00Z", "2026-01-02T09:41:00Z"], "landed_24h": 4, "parked": 1},
  "flows": [{"from": "cost", "to": "qwen3.5:4b", "n": 10, "hot": 3}, {"from": "cost", "to": "qwen3.5:9b", "n": 5, "hot": 1},
    {"from": "card grow", "to": "qwen3.5:4b", "n": 4, "hot": 1}, {"from": "review", "to": "claude", "n": 3, "hot": 0},
    {"from": "subagent:general-purpose", "to": "claude", "n": 2, "hot": 1}, {"from": "moon grow", "to": "jeff-coder", "n": 2, "hot": 0}],
  "board": {
    "at": "2026-01-02T10:00:00Z",
    "library": {"calling": 140, "hourly": [0, 0, 2, 5, 3, 0, 0, 0, 1, 4, 9, 12, 7, 3, 0, 0, 2, 6, 11, 15, 9, 4, 2, 6],
      "recent": [{"grid": "cards", "at": "2026-01-02T09:40:00Z", "kept": 6}]},
    "counts": {"cards": 1820, "ui": 640, "moons": 910, "calling": 140},
    "state": {"tree": [{"name": "tasks", "dir": true, "at": "2026-01-02T09:58:00Z", "n": 412, "kb": 0},
      {"name": "lanes", "dir": true, "at": "2026-01-02T09:30:00Z", "n": 6, "kb": 0},
      {"name": "cache", "dir": true, "at": "2026-01-02T09:59:00Z", "n": 3, "kb": 0},
      {"name": "ledger.jsonl", "dir": false, "at": "2026-01-02T09:59:30Z", "n": 0, "kb": 3310},
      {"name": "decisions.md", "dir": false, "at": "2026-01-02T08:10:00Z", "n": 0, "kb": 84},
      {"name": "meta.json", "dir": false, "at": "2026-01-02T09:50:00Z", "n": 0, "kb": 2}],
      "newest": "ledger.jsonl",
      "doc": {"kind": "decision", "title": "Retry a slow login once, then say so", "at": "2026-01-02",
        "text": "One retry covers the slow links we measured (p95 2.1 s); a second only hid real outages. The message names the retry so support can tell them apart."}},
    "tests": {"modules": [
      {"name": "test_cli", "state": "cached"}, {"name": "test_core", "state": "cached"}, {"name": "test_login", "state": "running"},
      {"name": "test_retry", "state": "fail"}, {"name": "test_ui", "state": "cached"}, {"name": "test_board", "state": "cached"},
      {"name": "test_cpu", "state": "cached"}, {"name": "test_docs", "state": "idle"}, {"name": "test_lanes", "state": "cached"},
      {"name": "test_merge", "state": "cached"}, {"name": "test_train", "state": "running"}, {"name": "test_bus", "state": "cached"},
      {"name": "test_ward", "state": "cached"}, {"name": "test_hooks", "state": "cached"}, {"name": "test_cost", "state": "idle"},
      {"name": "test_live", "state": "cached"}, {"name": "test_moons", "state": "idle"}, {"name": "test_cards", "state": "cached"},
      {"name": "test_shot", "state": "cached"}, {"name": "test_sync", "state": "cached"}, {"name": "test_map", "state": "cached"},
      {"name": "test_graph", "state": "cached"}, {"name": "test_intake", "state": "cached"}, {"name": "test_queue", "state": "cached"}],
      "done": 9, "total": 10, "running": 2, "source": "last check 09:52"},
    "cpu": {"kinds": ["tests", "claude", "models", "hooks", "other"], "goal_pct": 10, "cores": 16,
      "minutes": [[1767347400, 0.0, 1.42, 0.8, 0.13, 1.1], [1767347460, 0.0, 1.47, 0.78, 0.08, 1.11], [1767347520, 0.38, 1.5, 0.71, 0.08, 1.13], [1767347580, 0.89, 1.49, 0.61, 0.13, 1.14], [1767347640, 1.28, 1.45, 0.52, 0.08, 1.16], [1767347700, 1.4, 1.39, 0.44, 0.08, 1.17], [1767347760, 1.2, 1.3, 0.4, 0.13, 1.18], [1767347820, 0.76, 1.19, 0.41, 0.08, 1.2], [1767347880, 0.25, 1.07, 0.47, 0.08, 1.21], [1767347940, 0.0, 0.95, 0.56, 0.13, 1.22], [1767348000, 0.0, 0.82, 0.66, 0.08, 1.24], [1767348060, 0.05, 0.71, 0.74, 0.08, 1.25], [1767348120, 0.51, 0.62, 1.29, 0.13, 1.27], [1767348180, 1.01, 0.55, 1.3, 0.08, 1.28], [1767348240, 1.34, 0.51, 1.25, 0.08, 1.29], [1767348300, 1.37, 0.5, 1.17, 0.13, 1.31], [1767348360, 1.1, 0.52, 1.07, 0.08, 1.32], [1767348420, 0.62, 0.57, 0.98, 0.08, 1.33], [1767348480, 0.14, 0.65, 0.92, 0.13, 1.35], [1767348540, 0.0, 0.75, 0.9, 0.08, 1.36], [1767348600, 0.0, 0.86, 0.43, 0.08, 1.38], [1767348660, 0.16, 0.98, 0.5, 0.13, 1.39], [1767348720, 0.65, 1.11, 0.6, 0.08, 1.4], [1767348780, 1.12, 1.23, 0.7, 0.08, 1.42], [1767348840, 1.38, 1.33, 0.77, 0.13, 1.43], [1767348900, 1.33, 1.41, 0.8, 0.08, 1.44], [1767348960, 0.99, 1.47, 0.78, 0.08, 1.46], [1767349020, 0.49, 1.5, 0.72, 0.13, 1.47], [1767349080, 0.03, 1.49, 0.63, 0.08, 1.49], [1767349140, 0.0, 1.46, 0.53, 0.08, 1.5]],
      "now": {"tests": 0.6, "claude": 1.4, "models": 0.9, "hooks": 0.1, "other": 1.5, "busy": 4.5}},
    "commands": [{"at": "2026-01-02T09:51:00Z", "cmd": "sampo next", "ok": true},
      {"at": "2026-01-02T09:53:00Z", "cmd": "sampo task show T-0007", "ok": true},
      {"at": "2026-01-02T09:55:00Z", "cmd": "sampo prove T-0007", "ok": false},
      {"at": "2026-01-02T09:58:00Z", "cmd": "sampo task evidence T-0007 --step 3", "ok": true},
      {"at": "2026-01-02T09:59:00Z", "cmd": "sampo check --affected", "ok": true}]
  },
  "budget": {"today_usd": 3.2, "subagent_tokens": 120000, "caps": {"day": 20, "run": 5}, "halved": null},
  "cpu": {"goal": 10, "cores": 16, "step_s": 150, "now": 6.4, "sessions": [
    {"name": "kitty-1001", "pct": [3.1, 2.8, 4.0, 5.2, 3.3, 2.1, 2.5, 3.9, 4.4, 3.0, 2.2, 3.1]},
    {"name": "kitty-1002", "pct": [1.0, 1.4, 2.2, 6.1, 7.0, 4.2, 1.1, 0.8, 2.0, 2.6, 1.9, 2.4]},
    {"name": "kitty-1003", "pct": [0.0, 0.0, 0.4, 0.9, 1.2, 0.6, 0.3, 0.2, 0.5, 0.7, 0.9, 0.9]}
  ]},
  "today_done": 4,
  "done_days": [0, 1, 0, 2, 0, 0, 3, 1, 0, 0, 1, 0, 2, 4],
  "typical": {"FIX/M": 40}
}
