"""T-5022 (slice of T-4325): the fold's second reader. `sampo fold check` samples the rows the fold ledger calls `built`
and lets an independent reader mark each one `ok` or `refuted`; a refuted row goes back to `stays`.

The ledger is fold.json in the project's state folder (p.dir), shared with the fold page (T-5020) and pinned here:
    {"rows": {"T-0001": {"verdict": "joined|merged|dropped|built|stays", "to": "", "evidence": "path:line or empty",
                         "why": "", "checked": null | "ok" | "refuted"}}}
This module only reads it and rewrites the verdict and `checked` of the row it marks, under the project lock, in one
atomic write; any other key (of a row or of the file) is kept as it was found.

No model judges here (models advise, proof decides): the sample is a seeded pick of unchecked built rows, printed with
their evidence and whether the cited file and line exist in the repo; the reader opens the evidence and marks. The
evidence is text from the ledger, so it is never trusted as a path: an absolute path, one with `..` that leaves the
repo, or one through a link that leaves it is reported as missing, and nothing outside the repo is opened."""
import json
import math
import os
import random
import re

from sampo import core as c

LEDGER = "fold.json"
VERDICTS = ("joined", "merged", "dropped", "built", "stays")
MARKS = ("ok", "refuted")
DEFAULT_PCT = 10  # of the unchecked built rows
MIN_SAMPLE = 3
MAX_SAMPLE = 500  # --n above this is a typo, not a sample
MAX_LEDGER = 20_000_000  # bytes read from fold.json
MAX_EVIDENCE_FILE = 5_000_000  # bytes counted for a cited file's lines
WHY_CHARS = 300
ID_CHARS = 40  # a ledger key is hand-editable text: shown cleaned and cut, looked up by the real key

# path, path:N or path:first-last (what fold set accepts, foldpage._EVIDENCE); a number past LINE_DIGITS is a line no file has
_EVIDENCE = re.compile(r"^(?P<path>.+?)(?::(?P<first>\d+)(?:-(?P<last>\d+))?)?$")
LINE_DIGITS = 9


def _usage(msg):
    from sampo import cli
    return cli.UsageError(msg)


def _clean(text, limit=300):
    s = c.plain(str(text if text is not None else "")).encode("utf-8", "replace").decode("utf-8")  # lone surrogates -> ?
    return s[:limit]


def ledger_path(p):
    return os.path.join(p.dir, LEDGER)


def load(p):
    """The ledger as a dict with a `rows` dict; a missing file is an empty ledger, a damaged one is an error (a write
    would put the loss back)."""
    path = ledger_path(p)
    try:
        if os.path.getsize(path) > MAX_LEDGER:
            raise _usage(f"{path} is over {MAX_LEDGER} bytes; Sampo won't read it as a fold ledger")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"rows": {}}
    except (OSError, ValueError) as e:
        raise _usage(f"{path} isn't a readable fold ledger ({e}); fix or restore it by hand") from None
    if not isinstance(data, dict) or not isinstance(data.get("rows", {}), dict):
        raise _usage(f"{path} isn't a fold ledger: expected {{\"rows\": {{ID: row}}}}")
    data.setdefault("rows", {})
    return data


def _is_built(row):
    return isinstance(row, dict) and row.get("verdict") == "built"


def candidates(rows):
    """Ids of built rows nobody has checked yet, in a fixed order."""
    return sorted(k for k, r in rows.items() if _is_built(r) and r.get("checked") in (None, ""))


def sample_size(total, n=None):
    if n is not None:
        return min(total, n)
    return min(total, max(MIN_SAMPLE, math.ceil(total * DEFAULT_PCT / 100)))


def sample(rows, n=None, seed=0):
    """A deterministic pick: the same ledger, n and seed give the same ids, in id order."""
    cands = candidates(rows)
    picked = random.Random(f"fold-check:{seed}").sample(cands, sample_size(len(cands), n))
    return sorted(picked)


def look(root, evidence):
    """What the evidence cites: {"file": bool, "line": bool, "path": str, "n": int|None}. A path that leaves the repo
    is reported as missing and never opened."""
    out = {"file": False, "line": False, "path": "", "n": None}
    m = _EVIDENCE.match(str(evidence or "").strip())
    if not m or not root:
        return out
    rel = m.group("path")
    out["path"] = _clean(rel)
    if os.path.isabs(rel) or "\x00" in rel:
        return out
    try:
        base = os.path.realpath(root)
        full = os.path.realpath(os.path.join(base, rel))
        if full != base and not full.startswith(base + os.sep):
            return out
        if not os.path.isfile(full):
            return out
        out["file"] = True
        if m.group("first") is None:
            return out
        digits = [d for d in (m.group("first"), m.group("last")) if d is not None]
        if any(len(d) > LINE_DIGITS for d in digits):  # a file does not have that many lines; never int() a 5000-digit run
            return out
        first = int(digits[0])
        last = int(digits[-1])
        out["n"] = first
        if os.path.getsize(full) > MAX_EVIDENCE_FILE:
            return out
        with open(full, "rb") as f:
            data = f.read()
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        out["line"] = 1 <= first <= last <= lines
    except (OSError, ValueError):
        pass
    return out


def build(p, n=None, seed=0):
    data = load(p)
    rows = data["rows"]
    built = [k for k, r in rows.items() if _is_built(r)]
    picks = []
    for tid in sample(rows, n, seed):
        r = rows[tid]
        seen = look(p.root, r.get("evidence"))
        picks.append({"id": _clean(tid, ID_CHARS), "to": _clean(r.get("to"), 80), "evidence": _clean(r.get("evidence")),
                      "why": _clean(r.get("why")), "file_exists": seen["file"], "line_exists": seen["line"]})
    done = [r.get("checked") for r in rows.values() if isinstance(r, dict)]
    return {"seed": seed, "built": len(built), "unchecked": len(candidates(rows)), "ok": done.count("ok"),
            "refuted": done.count("refuted"), "sample": picks}


def render(res):
    if not res["built"]:
        return "fold check: no built verdicts in the ledger (sampo fold set ID built ...)"
    if not res["sample"]:
        return (f"fold check: every one of the {res['built']} built verdict(s) is already checked "
                f"({res['ok']} ok, {res['refuted']} refuted)")
    lines = [f"fold check: {len(res['sample'])} of {res['unchecked']} unchecked built verdict(s), seed {res['seed']} "
             f"({res['ok']} ok, {res['refuted']} refuted so far)"]
    for x in res["sample"]:
        if not x["evidence"]:
            where = "no evidence cited"
        elif x["file_exists"] and x["line_exists"]:
            where = "file and line exist"
        elif x["file_exists"]:
            where = "file exists, the line does not (or none cited)"
        else:
            where = "file NOT found in the repo"
        lines.append(f"  {x['id']}  {x['evidence'] or '-'}  [{where}]" + (f"  -> {x['to']}" if x["to"] else ""))
    lines.append("open each, then: sampo fold check --mark ID ok|refuted [--why TEXT]")
    return "\n".join(lines)


def mark(p, tid, verdict, why=None):
    """Record the second reader's verdict on one built row. ok keeps the row; refuted sends it back to stays with the
    reason. Only a built row is marked: a refuted one is already back at stays."""
    tid = str(tid).strip().upper()  # as fold set normalises its ids
    shown = _clean(tid, ID_CHARS)
    if verdict not in MARKS:
        raise _usage(f"mark {shown} ok or refuted, not {_clean(verdict, 20)!r}")
    with c.lock(p.dir):
        data = load(p)
        row = data["rows"].get(tid)
        if row is None:
            raise _usage(f"no row {shown} in the fold ledger")
        if not _is_built(row):
            raise _usage(f"{shown} is {_clean(row.get('verdict'), 20) if isinstance(row, dict) else 'not a row'}, not built: "
                         "only a built verdict is checked")
        row["checked"] = verdict
        if verdict == "refuted":
            ev = _clean(row.get("evidence"), 120)
            row["verdict"] = "stays"
            row["why"] = c.defang(_clean(re.sub(r"\s+", " ", str(why)).strip(), WHY_CHARS)) if why else f"refuted by the second reader (built, cited {ev or 'no evidence'})"
        # the page's settings (indent=1, sort_keys, UTF-8), so a mark changes only its row's lines; a lone surrogate (a
        # \udcff escape in the file) cannot be encoded, so such a file keeps its escapes: the write never fails on it
        text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            text = json.dumps(data, indent=1, sort_keys=True) + "\n"
        c.write_atomic(ledger_path(p), text)
    return {"id": shown, "checked": verdict, "verdict": row["verdict"], "why": row.get("why", "")}


def cmd_check(args):
    """sampo fold check [--n N] [--seed S] [--json] [--mark ID ok|refuted [--why TEXT]]"""
    from sampo import cli
    p = cli.resolve(args, create=False)
    if getattr(args, "words", None):
        raise cli.UsageError(f"sampo fold check takes no words: {_clean(' '.join(map(str, args.words)), 60)}")
    pair = getattr(args, "mark", None)
    if pair:
        if len(pair) != 2:
            raise cli.UsageError("sampo fold check --mark ID ok|refuted")
        res = mark(p, pair[0], str(pair[1]).strip().lower(), getattr(args, "why", None))
        cli.out(args, res, f"{res['id']}: checked {res['checked']}"
                + (f", back to stays ({res['why']})" if res["checked"] == "refuted" else ""))
        return 0
    n = getattr(args, "n", None)
    if n is not None and not 1 <= n <= MAX_SAMPLE:
        raise cli.UsageError(f"--n takes a number from 1 to {MAX_SAMPLE}")
    res = build(p, n, getattr(args, "seed", None) or 0)
    cli.out(args, res, render(res))
    return 0
