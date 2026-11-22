"""T-5020 (slice of T-4325): the fold ledger. `sampo fold set ID VERDICT [--to ID] [--evidence path:line] [--why T]`
records one verdict for a task; `sampo fold page` prints every captured and planned task (the inbox and the queue) with its
verdict or 'unfolded'; `sampo fold [--json]` counts them, and counts the standalone ones (verdict stays, or no verdict).
`sampo fold check` is T-5022's second reader (foldcheck.cmd_check).

The ledger is one file per project, fold.json in the project's state folder (task ids repeat across projects), apart from the briefs (no brief is rewritten, so a live lane's
task file is never raced). Its schema is shared with foldcheck and does not change without it:
  {"rows": {"T-0001": {"verdict": "joined|merged|dropped|built|stays", "to": "T-0002 or empty",
                        "evidence": "path:line or empty", "why": "", "checked": null | "ok" | "refuted"}}}
"""
import json
import os
import re
import sys

from sampo import core as c

VERDICTS = ("joined", "merged", "dropped", "built", "stays")
OPEN = ("captured", "planned")  # the inbox and the queue
WHY_MAX = 300
_ID = re.compile(r"^T-\d+$")
_EVIDENCE = re.compile(r"^[^\s:\x00][^:\x00\n]*:\d+(?:-\d+)?$")  # path:line or path:first-last


def ledger_path(p):
    """One ledger per project: task ids (T-0001) repeat across projects, so a shared file would cross their verdicts."""
    return os.path.join(p.dir, "fold.json")


def load(p):
    """The ledger's rows ({id: row}); no file is no rows. A file that isn't a ledger raises UsageError naming it: a
    write over it would drop what the verdicts in it say."""
    from sampo import cli
    path = ledger_path(p)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise cli.UsageError(f"{path} is not readable as the fold ledger ({e}); fix or move it, nothing was changed")
    rows = d.get("rows") if isinstance(d, dict) else None
    if not isinstance(rows, dict):
        raise cli.UsageError(f"{path} has no \"rows\" object; fix or move it, nothing was changed")
    return rows


def _safe(v):
    """A ledger value for printing: strings lose control and bidi characters (the file may be hand-written)."""
    return c.plain(v) if isinstance(v, str) else v


def verdict_of(row):
    """The verdict a ledger row gives, or None when the row is no ruling (junk, an unknown word)."""
    v = row.get("verdict") if isinstance(row, dict) else None
    return v if v in VERDICTS else None


def _line(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _norm_id(text, what="task"):
    from sampo import cli
    tid = str(text or "").strip().upper()
    if not _ID.match(tid):
        raise cli.UsageError(f"{text!r} is not a task id (T-0123) for the {what}")
    return tid


def set_verdict(p, tid, verdict, to="", evidence="", why=""):
    """Records the verdict; returns the row. Raises UsageError, writing nothing, on input that is no ruling."""
    from sampo import cli
    tid = _norm_id(tid)
    verdict = str(verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise cli.UsageError(f"{verdict!r} is not a verdict: {', '.join(VERDICTS)}")
    known = {b.id for b in c.load_briefs(p, include_archive=True)}
    if tid not in known:
        raise cli.UsageError(f"no task {tid} in this project")
    to = str(to or "").strip()
    if to:
        to = _norm_id(to, "--to")
        if verdict not in ("joined", "merged"):
            raise cli.UsageError("--to names the task a verdict of joined or merged points at")
        if to == tid:
            raise cli.UsageError(f"{tid} cannot be {verdict} into itself")
        if to not in known:
            raise cli.UsageError(f"--to {to}: no such task in this project")
    elif verdict in ("joined", "merged"):
        raise cli.UsageError(f"a verdict of {verdict} needs --to ID: the task it went into")
    evidence = c.plain(str(evidence or "")).strip()  # cleaned as --why is: the set echo prints it
    if evidence and not _EVIDENCE.match(evidence):
        raise cli.UsageError(f"--evidence {evidence!r} is not path:line (or path:first-last)")
    if verdict == "built" and not evidence:
        raise cli.UsageError("a verdict of built needs --evidence path:line: where the work already is")
    row = {"verdict": verdict, "to": to, "evidence": evidence, "why": c.defang(c.plain(_line(why)))[:WHY_MAX], "checked": None}
    with c.lock(p.dir):  # two sets at once each keep their row
        rows = load(p)
        rows[tid] = row
        c.write_atomic(ledger_path(p), json.dumps({"rows": rows}, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return row


def snapshot(p):
    """What the page and the counts read: the open tasks (inbox, then queue, by id) each with its row's verdict, the
    counts, and the ledger's rows for tasks no longer open (kept: a verdict never vanishes with its task)."""
    rows = load(p)
    open_ = sorted((b for b in c.load_briefs(p) if b.status in OPEN), key=lambda b: (int(b.id[2:]) if b.id[2:].isdigit() else 0, b.id))
    tasks = []
    for b in open_:
        row = rows.get(b.id)
        v = verdict_of(row)
        tasks.append({"id": b.id, "status": b.status, "type": b.type, "tier": b.tier, "title": _line(b.title),
                      "verdict": v or "unfolded", **({"to": _safe(row.get("to") or ""), "evidence": _safe(row.get("evidence") or ""),
                                                      "why": _safe(row.get("why") or ""), "checked": _safe(row.get("checked"))} if v else {})})
    here = {t["id"] for t in tasks}
    gone = [{"id": c.plain(str(k)), **{f: (_safe(rows[k].get(f)) if isinstance(rows[k], dict) else None) for f in ("verdict", "to", "evidence", "why", "checked")}}
            for k in sorted(rows) if k not in here and verdict_of(rows[k])]
    verdicts = {v: 0 for v in VERDICTS}
    verdicts["unfolded"] = 0
    for t in tasks:
        verdicts[t["verdict"]] += 1
    return {"open": len(tasks), "captured": sum(t["status"] == "captured" for t in tasks),
            "planned": sum(t["status"] == "planned" for t in tasks), "verdicts": verdicts,
            "standalone": verdicts["stays"] + verdicts["unfolded"], "gone": len(gone), "tasks": tasks, "closed_rows": gone}


def _item(t):
    s = f"- {t['id']} [{t['type']} {t['tier']}] {t['title']}: "
    if t["verdict"] == "unfolded":
        return s + "unfolded"
    s += f"**{t['verdict']}**" + (f" → {t['to']}" if t.get("to") else "") + (f" ({t['evidence']})" if t.get("evidence") else "")
    if t.get("checked"):
        s += f" [{t['checked']}]"
    return s + (f" — {t['why']}" if t.get("why") else "")


def summary(d):
    v = d["verdicts"]
    done = ", ".join(f"{v[k]} {k}" for k in VERDICTS if v[k])
    return (f"{d['open']} open ({d['captured']} captured, {d['planned']} planned): "
            f"{done or 'no verdicts yet'}, {v['unfolded']} unfolded; standalone {d['standalone']} "
            f"(stays {v['stays']} plus unfolded {v['unfolded']})")


def page(d):
    out = ["# Fold ledger", "", summary(d), ""]
    for status, head in (("captured", "Inbox (captured)"), ("planned", "Queue (planned)")):
        items = [t for t in d["tasks"] if t["status"] == status]
        out += [f"## {head}: {len(items)}", ""] + [_item(t) for t in items] + [""]
    if d["closed_rows"]:
        out += [f"## Verdicts on tasks no longer open: {len(d['closed_rows'])}", ""]
        for r in d["closed_rows"]:
            out.append(f"- {r['id']}: **{r['verdict']}**" + (f" → {r['to']}" if r.get("to") else "")
                       + (f" — {_line(r['why'])}" if r.get("why") else ""))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def cmd_fold(args):
    from sampo import cli
    action = args.action
    if action == "check":  # T-5022's reader: built when its module is there
        import importlib
        try:
            foldcheck = importlib.import_module("sampo.foldcheck")
        except ModuleNotFoundError as e:  # only its own absence: a module it imports that is missing is a real fault
            if e.name != "sampo.foldcheck":
                raise
            print("sampo fold check: not built yet (T-5022 supplies it)")
            return 1
        return foldcheck.cmd_check(args)
    p = cli.resolve(args, create=False)
    if action == "set":
        if len(args.words) != 2:
            raise cli.UsageError("usage: sampo fold set ID VERDICT [--to ID] [--evidence path:line] [--why TEXT]")
        tid, verdict = args.words
        row = set_verdict(p, tid, verdict, to=args.to, evidence=args.evidence, why=args.why)
        return cli.out(args, {"id": _norm_id(tid), **row},
                       f"{_norm_id(tid)}: {row['verdict']}" + (f" → {row['to']}" if row["to"] else "")
                       + (f" ({row['evidence']})" if row["evidence"] else ""))
    if args.words:
        raise cli.UsageError(f"sampo fold {action or ''} takes no arguments: {' '.join(args.words)}")
    d = snapshot(p)
    if action == "page":
        return cli.out(args, d, page(d).rstrip("\n"))
    return cli.out(args, d, summary(d))
