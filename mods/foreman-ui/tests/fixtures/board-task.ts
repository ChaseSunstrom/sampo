// T-3701: board-busy.ts with no lanes out, no train and no model called in the last minute: the task in focus
// and the quiet panels. Synthetic; plain JSON after the export, so tools read it as data too.
export default {
 "v": 1,
 "project": "demo-1a2b3c",
 "root": "/repo",
 "mode": {
  "autonomy": "full",
  "drive": true,
  "sensitive": false
 },
 "active": {
  "id": "T-0007",
  "type": "FIX",
  "tier": "M",
  "title": "Login times out on slow links",
  "status": "active",
  "stage": "verifying",
  "words": "the login keeps timing out for people on slow links: make it retry once and tell them it did, without hiding real outages",
  "stages": [
   "planning",
   "ready",
   "executing",
   "verifying",
   "auditing",
   "closing"
  ],
  "steps": [
   {
    "n": 1,
    "text": "red test: a 6 s link logs in",
    "done": true,
    "current": false
   },
   {
    "n": 2,
    "text": "raise the timeout to 20 s",
    "done": true,
    "current": false
   },
   {
    "n": 3,
    "text": "retry once on a slow link",
    "done": false,
    "current": true
   }
  ],
  "criteria": [
   {
    "n": 1,
    "text": "slow wifi logs in",
    "verify": "pytest -q tests/test_login.py",
    "checked": true
   },
   {
    "n": 2,
    "text": "a dead link fails in 30 s",
    "verify": "pytest -q tests/test_login.py",
    "checked": false
   }
  ],
  "audits": {
   "done": 1,
   "need": 2
  },
  "blockers": [],
  "on_task_s": 1500
 },
 "next": "T-0007 step 3/3: retry once on a slow link",
 "lanes": {
  "building": [],
  "merging": [],
  "closing": []
 },
 "queue": [
  {
   "id": "T-0008",
   "type": "CLEAN",
   "tier": "S",
   "title": "Tidy helpers",
   "status": "planned",
   "steps_done": 1,
   "steps_total": 3
  },
  {
   "id": "T-0010",
   "type": "SECURITY",
   "tier": "M",
   "title": "Redact tokens in the log",
   "status": "planned",
   "steps_done": 0,
   "steps_total": 4
  },
  {
   "id": "T-0009",
   "type": "FEATURE",
   "tier": "L",
   "title": "CSV export",
   "status": "planned",
   "waits": "plan approval",
   "plan": {
    "interpretation": "export the report table",
    "approach": "csv module vs pandas: csv module",
    "steps": [
     {
      "n": 1,
      "text": "write the exporter",
      "done": false,
      "current": true
     }
    ],
    "criteria": [
     {
      "n": 1,
      "text": "opens in a spreadsheet",
      "verify": "pytest -q",
      "checked": false
     }
    ]
   }
  }
 ],
 "queue_total": 12,
 "inbox": [
  {
   "id": "T-0011",
   "type": "CLEAN",
   "tier": "S",
   "title": "Merge date helpers",
   "status": "captured",
   "age_days": 2,
   "source": "user"
  },
  {
   "id": "T-0012",
   "type": "RESEARCH",
   "tier": "S",
   "title": "Which chart lib",
   "status": "captured",
   "age_days": 9
  }
 ],
 "inbox_total": 5,
 "inbox_yours": 1,
 "approvals": [],
 "closed": [
  {
   "id": "T-0006",
   "status": "done",
   "title": "Fix the flaky clock test",
   "grade": "ok",
   "lenses": [
    "edge"
   ]
  }
 ],
 "recent": [],
 "history": [
  {
   "time": "09:12",
   "mark": "⚑",
   "task": "T-0011",
   "text": "captured",
   "title": "Merge date helpers"
  },
  {
   "time": "09:40",
   "mark": "✓",
   "task": "T-0006",
   "text": "done",
   "title": "Fix the flaky clock test"
  },
  {
   "time": "10:05",
   "mark": "✗",
   "task": "T-0007",
   "text": "step 1 failed",
   "title": "Login times out on slow links"
  },
  {
   "time": "10:09",
   "mark": "✓",
   "task": "T-0007",
   "text": "step 1 passed",
   "title": "Login times out on slow links"
  },
  {
   "time": "10:31",
   "mark": "◆",
   "task": "T-0021",
   "text": "lane started",
   "title": "Live page chart legend"
  },
  {
   "time": "10:48",
   "mark": "✓",
   "task": "T-0007",
   "text": "step 2 passed",
   "title": "Login times out on slow links"
  },
  {
   "time": "11:02",
   "mark": "✓",
   "task": "T-0024",
   "text": "lane finished",
   "title": "Cache the view build"
  },
  {
   "time": "11:20",
   "mark": "✓",
   "task": "T-0019",
   "text": "merged",
   "title": "Flaky clock test"
  }
 ],
 "fleet": [
  {
   "session": "a1b2c3d4e5f6",
   "project": "demo-1a2b3c",
   "task": "T-0007",
   "title": "Login times out on slow links",
   "context_pct": 41,
   "model": "Opus 5.5",
   "seen": "2026-01-02T10:00:00Z"
  },
  {
   "session": "b7c8d9e0f1a2",
   "project": "demo-1a2b3c",
   "task": "T-0021",
   "title": "Live page chart legend",
   "context_pct": 63,
   "mail": 1,
   "model": "Sonnet 5",
   "seen": "2026-01-02T09:59:30Z"
  },
  {
   "session": "c3d4e5f6a7b8",
   "project": "shop-9f8e7d",
   "task": null,
   "title": null,
   "context_pct": 12,
   "model": "Opus 5.5",
   "seen": "2026-01-02T09:40:00Z"
  },
  {
   "session": "d4e5f6a7b8c9",
   "project": "notes-4c5d6e",
   "task": "T-0102",
   "title": "Sync the inbox",
   "context_pct": 86,
   "model": "Haiku 4.5",
   "seen": "2026-01-02T09:59:50Z"
  },
  {
   "session": "e5f6a7b8c9d0",
   "project": "shop-9f8e7d",
   "task": "T-0311",
   "title": "Checkout totals",
   "context_pct": 27,
   "model": "Sonnet 5",
   "seen": "2026-01-02T09:58:40Z"
  }
 ],
 "worktrees": [],
 "health": {
  "hook_p95_ms": 31,
  "guard_blocks": 0,
  "hook_errors": 0
 },
 "watch": [],
 "latency": [
  20,
  25,
  31,
  22,
  40,
  28
 ],
 "checks": {
  "at": "2026-10-02T19:40:00Z",
  "results": [
   {
    "cmd": "python3 -m unittest -q",
    "exit": 0,
    "s": 140.2
   },
   {
    "cmd": "claude plugin test mods/foreman-ui",
    "exit": 0,
    "s": 66.0
   },
   {
    "cmd": "ruff check",
    "exit": 0,
    "s": 1.2
   },
   {
    "cmd": "tsc -p mods/foreman-ui",
    "exit": 0,
    "s": 9.8
   },
   {
    "cmd": "docs drift",
    "exit": 0,
    "s": 3.1
   },
   {
    "cmd": "bench",
    "exit": 1,
    "s": 11.0,
    "note": "slower than usual"
   }
  ]
 },
 "train": {
  "running": false,
  "landed": [],
  "landed_24h": 0,
  "parked": 0
 },
 "flows": [
  {
   "from": "cost",
   "to": "qwen3.5:4b",
   "n": 10,
   "hot": 0
  },
  {
   "from": "cost",
   "to": "qwen3.5:9b",
   "n": 5,
   "hot": 0
  },
  {
   "from": "card grow",
   "to": "qwen3.5:4b",
   "n": 4,
   "hot": 0
  },
  {
   "from": "review",
   "to": "claude",
   "n": 3,
   "hot": 0
  },
  {
   "from": "subagent:general-purpose",
   "to": "claude",
   "n": 2,
   "hot": 0
  },
  {
   "from": "moon grow",
   "to": "jeff-coder",
   "n": 2,
   "hot": 0
  }
 ],
 "board": {
  "at": "2026-01-02T10:00:00Z",
  "library": {
   "calling": 140,
   "hourly": [
    0,
    0,
    2,
    5,
    3,
    0,
    0,
    0,
    1,
    4,
    9,
    12,
    7,
    3,
    0,
    0,
    2,
    6,
    11,
    15,
    9,
    4,
    2,
    6
   ],
   "recent": [
    {
     "grid": "cards",
     "at": "2026-01-02T09:40:00Z",
     "kept": 6
    }
   ]
  },
  "counts": {
   "cards": 1820,
   "ui": 640,
   "moons": 910,
   "calling": 140
  },
  "state": {
   "tree": [
    {
     "name": "tasks",
     "dir": true,
     "at": "2026-01-02T09:58:00Z",
     "n": 412,
     "kb": 0
    },
    {
     "name": "lanes",
     "dir": true,
     "at": "2026-01-02T09:30:00Z",
     "n": 6,
     "kb": 0
    },
    {
     "name": "cache",
     "dir": true,
     "at": "2026-01-02T09:59:00Z",
     "n": 3,
     "kb": 0
    },
    {
     "name": "ledger.jsonl",
     "dir": false,
     "at": "2026-01-02T09:59:30Z",
     "n": 0,
     "kb": 3310
    },
    {
     "name": "decisions.md",
     "dir": false,
     "at": "2026-01-02T08:10:00Z",
     "n": 0,
     "kb": 84
    },
    {
     "name": "meta.json",
     "dir": false,
     "at": "2026-01-02T09:50:00Z",
     "n": 0,
     "kb": 2
    }
   ],
   "newest": "ledger.jsonl",
   "doc": {
    "kind": "decision",
    "title": "Retry a slow login once, then say so",
    "at": "2026-01-02",
    "text": "One retry covers the slow links we measured (p95 2.1 s); a second only hid real outages. The message names the retry so support can tell them apart."
   }
  },
  "tests": {
   "modules": [
    {
     "name": "test_cli",
     "state": "cached"
    },
    {
     "name": "test_core",
     "state": "cached"
    },
    {
     "name": "test_login",
     "state": "running"
    },
    {
     "name": "test_retry",
     "state": "fail"
    },
    {
     "name": "test_ui",
     "state": "cached"
    },
    {
     "name": "test_board",
     "state": "cached"
    },
    {
     "name": "test_cpu",
     "state": "cached"
    },
    {
     "name": "test_docs",
     "state": "idle"
    },
    {
     "name": "test_lanes",
     "state": "cached"
    },
    {
     "name": "test_merge",
     "state": "cached"
    },
    {
     "name": "test_train",
     "state": "running"
    },
    {
     "name": "test_bus",
     "state": "cached"
    },
    {
     "name": "test_ward",
     "state": "cached"
    },
    {
     "name": "test_hooks",
     "state": "cached"
    },
    {
     "name": "test_cost",
     "state": "idle"
    },
    {
     "name": "test_live",
     "state": "cached"
    },
    {
     "name": "test_moons",
     "state": "idle"
    },
    {
     "name": "test_cards",
     "state": "cached"
    },
    {
     "name": "test_shot",
     "state": "cached"
    },
    {
     "name": "test_sync",
     "state": "cached"
    },
    {
     "name": "test_map",
     "state": "cached"
    },
    {
     "name": "test_graph",
     "state": "cached"
    },
    {
     "name": "test_intake",
     "state": "cached"
    },
    {
     "name": "test_queue",
     "state": "cached"
    }
   ],
   "done": 9,
   "total": 10,
   "running": 2,
   "source": "last check 09:52"
  },
  "commands": [
   {
    "at": "2026-01-02T09:51:00Z",
    "cmd": "sampo next",
    "ok": true
   },
   {
    "at": "2026-01-02T09:53:00Z",
    "cmd": "sampo task show T-0007",
    "ok": true
   },
   {
    "at": "2026-01-02T09:55:00Z",
    "cmd": "sampo prove T-0007",
    "ok": false
   },
   {
    "at": "2026-01-02T09:58:00Z",
    "cmd": "sampo task evidence T-0007 --step 3",
    "ok": true
   },
   {
    "at": "2026-01-02T09:59:00Z",
    "cmd": "sampo check --affected",
    "ok": true
   }
  ]
 },
 "budget": {
  "today_usd": 3.2,
  "subagent_tokens": 120000,
  "caps": {
   "day": 20,
   "run": 5
  },
  "halved": null
 },
 "cpu": {
  "goal": 10,
  "cores": 16,
  "step_s": 150,
  "now": 6.4,
  "sessions": [
   {
    "name": "kitty-1001",
    "pct": [
     3.1,
     2.8,
     4.0,
     5.2,
     3.3,
     2.1,
     2.5,
     3.9,
     4.4,
     3.0,
     2.2,
     3.1
    ]
   },
   {
    "name": "kitty-1002",
    "pct": [
     1.0,
     1.4,
     2.2,
     6.1,
     7.0,
     4.2,
     1.1,
     0.8,
     2.0,
     2.6,
     1.9,
     2.4
    ]
   },
   {
    "name": "kitty-1003",
    "pct": [
     0.0,
     0.0,
     0.4,
     0.9,
     1.2,
     0.6,
     0.3,
     0.2,
     0.5,
     0.7,
     0.9,
     0.9
    ]
   }
  ]
 },
 "today_done": 4,
 "done_days": [
  0,
  1,
  0,
  2,
  0,
  0,
  3,
  1,
  0,
  0,
  1,
  0,
  2,
  4
 ],
 "typical": {
  "FIX/M": 40
 }
}
