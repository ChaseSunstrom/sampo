// T-1534: a synthetic `fm ui --json` view (no real project's data), the pane's whole input. Plain JSON after the
// export, so tools/pane_replay.py reads it as data too; keep it that way (no code, no comments below this line).
export default {
  "v": 1,
  "project": "demo-1a2b3c",
  "root": "/repo",
  "mode": {"autonomy": "standard", "drive": true, "sensitive": false},
  "active": {
    "id": "T-0007", "type": "FIX", "tier": "M", "title": "Login times out", "status": "active", "stage": "executing",
    "stages": ["planning", "ready", "executing", "verifying", "auditing", "closing"],
    "steps": [
      {"n": 1, "text": "red test", "done": true, "current": false},
      {"n": 2, "text": "raise the timeout", "done": false, "current": true},
      {"n": 3, "text": "retry once on a slow link", "done": false, "current": false}
    ],
    "criteria": [{"n": 1, "text": "slow wifi logs in", "verify": "pytest -q tests/test_login.py", "checked": false}],
    "audits": {"done": 0, "need": 2},
    "blockers": ["step 2 not done"],
    "on_task_s": 1500,
    "hypotheses": [
      {"n": 1, "status": "ruled out", "text": "the session cache is shared"},
      {"n": 2, "status": "open", "text": "the client gives up after 5 s"}
    ]
  },
  "next": "T-0007 step 2/3: raise the timeout",
  "queue": [
    {"id": "T-0008", "type": "CLEAN", "tier": "S", "title": "Tidy helpers", "status": "planned", "steps_done": 1, "steps_total": 3},
    {"id": "T-0009", "type": "FEATURE", "tier": "L", "title": "CSV export", "status": "planned", "waits": "plan approval",
     "plan": {"interpretation": "export the report table", "approach": "csv module vs pandas: csv module",
              "steps": [{"n": 1, "text": "write the exporter", "done": false, "current": true}],
              "criteria": [{"n": 1, "text": "opens in a spreadsheet", "verify": "pytest -q", "checked": false}]}}
  ],
  "inbox": [{"id": "T-0011", "type": "CLEAN", "tier": "S", "title": "Merge date helpers", "status": "captured", "age_days": 2}],
  "inbox_total": 1,
  "approvals": [],
  "closed": [{"id": "T-0006", "status": "done", "title": "Fix the flaky clock test", "grade": "ok", "lenses": ["edge"]}],
  "recent": ["13:12 ⚑ T-0011 captured", "13:05 ✓ T-0007 step 1/3 red test"],
  "health": {"hook_p95_ms": 31, "guard_blocks": 0, "hook_errors": 0},
  "watch": [],
  "latency": [20, 25, 31, 22, 40, 28],
  "checks": {"at": "2026-10-02T19:40:00Z", "results": [
    {"cmd": "python3 -m unittest -q", "exit": 0, "s": 140.2},
    {"cmd": "bench", "exit": 1, "s": 11.0, "note": "slower than usual"}
  ]},
  "today_done": 1,
  "done_days": [0, 1, 0, 2, 0, 0, 3, 1, 0, 0, 1, 0, 2, 1],
  "typical": {"FIX/M": 40}
}
