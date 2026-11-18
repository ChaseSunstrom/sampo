#!/usr/bin/env python3
"""Aggregates of Sampo's own history for the site's charts (T-1220): python3 site/stats.py [--state DIR] [--no-bench]

Reads this repo's Sampo state (the ledger, the task briefs) and git, and writes site/data/stats.json. Only counts,
categories and timings are written, plus the benchmark repos' short task names and the titles of work a marathon found
there (toy repos, published on purpose), and the tower's moonshot titles, waiting ideas and the first clause of each
one's blocker (T-2767, the Moons and Underworld pages, published on purpose, paths and quoted commands cut): never this
repo's task titles, a path, a command or a session id. CI has no Sampo state, so
this runs here and its output is committed; site/build.py only reads it."""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "site", "data", "stats.json")
TYPES = ["FEATURE", "FIX", "CLEAN", "PERFORMANCE", "SECURITY", "RESEARCH"]
HARNESS = ("foreman", "sampo")  # T-2767: the exam's name for the harness's own arm, before and after the rename


def git(*a):
    return subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True, check=True).stdout


def state_dir():
    """This repo's project: the foreman-* project folder with the biggest ledger (sandboxes and improve worktrees have
    their own, much smaller ones)."""
    home = os.environ.get("SAMPO_HOME") or os.environ.get("FOREMAN_HOME") or ROOT
    found = [d for d in glob.glob(os.path.join(home, "state", "projects", "foreman-*"))
             if os.path.isfile(os.path.join(d, "ledger.jsonl"))]
    return max(found, key=lambda d: os.path.getsize(os.path.join(d, "ledger.jsonl")), default=None)


def briefs(state):
    """{task id: (type, tier)} from every brief's frontmatter, archived ones included."""
    out = {}
    for path in glob.glob(os.path.join(state, "tasks", "*.md")) + glob.glob(os.path.join(state, "archive", "**", "*.md"),
                                                                            recursive=True):
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(1500)
        except OSError:
            continue
        tid = re.search(r"(?m)^id: (T-\d+)", head)
        typ = re.search(r"(?m)^type: (\w+)", head)
        tier = re.search(r"(?m)^tier: (\w+)", head)
        if tid and typ:
            out[tid.group(1)] = (typ.group(1), tier.group(1) if tier else "S")
    return out


def day(ts):
    return ts[:10]


def ledger(state, kinds):
    tasks = briefs(state)
    done_by_day = collections.defaultdict(collections.Counter)
    tiers, types = collections.Counter(), collections.Counter()
    guard, lenses, severities, agents = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    lanes_by_day, evidence_by_day, gate_secs = collections.Counter(), collections.Counter(), []
    totals = collections.Counter()
    seen_done = set()
    with open(os.path.join(state, "ledger.jsonl"), encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ev, ts, data = e.get("event"), e.get("ts") or "", e.get("data") or {}
            if not ts:
                continue
            totals[ev] += 1
            if ev == "task_done" and e.get("task") not in seen_done:
                seen_done.add(e.get("task"))
                typ, tier = tasks.get(e.get("task"), ("FEATURE", "S"))
                typ = typ if typ in TYPES else "FEATURE"
                done_by_day[day(ts)][typ] += 1
                tiers[tier] += 1
                types[typ] += 1
            elif ev == "guard_block":
                guard[str(data.get("category") or "other")] += 1
            elif ev == "finding":
                lenses[str(data.get("lens") or "other")] += 1
                if data.get("severity"):
                    severities[str(data["severity"]).upper()] += 1
            elif ev == "subagent":
                agents[str(data.get("agent_type") or "other").replace("foreman:", "")] += 1
            elif ev == "lane_merge":
                lanes_by_day[day(ts)] += 1
            elif ev == "evidence":
                evidence_by_day[day(ts)] += 1
            elif ev == "check_run":
                s = sum(r.get("s") or 0 for r in data.get("results") or [] if isinstance(r, dict))
                if s:
                    gate_secs.append(round(s, 1))
    days = sorted(set(done_by_day) | set(lanes_by_day) | set(evidence_by_day))
    return {
        "totals": {"tasks_done": len(seen_done), "evidence": totals["evidence"], "proofs": totals["prove"],
                   "commits": totals["commit"], "gate_runs": totals["check_run"], "subagents": totals["subagent"],
                   "lanes_merged": totals["lane_merge"], "findings": totals["finding"],
                   "guard_blocks": totals["guard_block"], "captures": totals["capture"],
                   "lessons_shown": totals["lesson_shown"]},
        "daily": [{"day": d, **{t: done_by_day[d][t] for t in TYPES}, "lanes": lanes_by_day[d],
                   "evidence": evidence_by_day[d]} for d in days],
        "tiers": {k: tiers[k] for k in ("S", "M", "L")},
        "types": {t: types[t] for t in TYPES},
        "guard": dict(guard.most_common(8)),
        "findings": {"lenses": dict(lenses.most_common(6)), "severity": dict(severities.most_common(4))},
        "subagents": dict(agents.most_common(6)),
        "gate_seconds": {"runs": len(gate_secs), "median": sorted(gate_secs)[len(gate_secs) // 2] if gate_secs else 0},
        "tools": {"misses": totals["capability_miss"], "resolved": totals["miss_resolved"],  # T-2378: #5's page
                  "proved": totals["tool_proved"], "searches": totals["tool_search"], "queued": totals["grant_queued"],
                  "refused": totals["grant_refused"]},
    }


def test_count(rev):
    """def test_ lines under plugin/tests at `rev`."""
    return sum(int(r.rsplit(":", 1)[1]) for r in git("grep", "-c", "def test_", rev, "--", "plugin/tests").splitlines())


def releases():
    out = []
    for line in git("log", "--format=%h %ad %s", "--date=short", "-E", "--grep=^Foreman [0-9]+\\.[0-9]+\\.[0-9]+$").splitlines()[:16]:
        sha, day, _, version = line.split(" ", 3)
        out.append({"version": version, "day": day, "tests": test_count(sha)})
    return list(reversed(out))


def cards():
    base = os.path.join(ROOT, "plugin", "skills", "library", "cards")
    return dict(sorted(((a, len(glob.glob(os.path.join(base, a, "*.md")))) for a in os.listdir(base)
                        if os.path.isdir(os.path.join(base, a))), key=lambda kv: -kv[1]))


def calibration():
    """T-2344: the Judgment page's figures, from fm outcomes --judgment --json; {} when it can't run here."""
    try:
        p = subprocess.run([os.path.join(ROOT, "plugin", "bin", "fm"), "outcomes", "--judgment", "--json"], cwd=ROOT,
                           capture_output=True, text=True, timeout=120)
        d = json.loads(p.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return {k: d[k] for k in ("scored", "pending", "brier", "bars", "races", "kinds", "interim") if k in d}  # T-2498


def decisions(state, limit=200):
    """T-2348: the Decisions page's tape: fm decide's rows (decisions.md in the project's state) newest first, each
    with its kind (costly or outward is a door), its stated confidence and the later row that reversed or revisited
    it (whole words, 4+ characters, as fmcore._settled reads them); [] when the file can't be read."""
    try:
        with open(os.path.join(state, "decisions.md"), encoding="utf-8", errors="replace") as f:
            rows = [x for x in f if x.startswith("| 2")]
    except OSError:
        return []
    parsed = []
    for row in rows:
        cells = re.split(r"(?<!\\)\|", row)
        if len(cells) >= 3:
            tags, text = re.match(r"^((?:\[[^\]]*\] )*)(.*)$", cells[2].strip()).groups()
            parsed.append((cells[1].strip(), tags, text))
    out = []
    for i, (date, tags, text) in enumerate(parsed):
        conf = re.search(r"\[confidence: (\d+)%\]", tags)
        later = next(({"date": d, "how": "reversed" if how == "reverses" else "revisited"}
                      for d, t, _ in parsed[i + 1:] for how, w in re.findall(r"\[(reverses|revisited): ([^\]]+)\]", t)
                      if len(w.strip()) >= 4 and re.search(r"(?<!\w)" + re.escape(w.strip().lower()) + r"(?!\w)",
                                                           text.lower())), None)
        out.append({"date": date, "text": text[:200], "later": later, "confidence": int(conf.group(1)) if conf else None,
                    "kind": next((k for k in ("costly", "outward") if f"[{k}]" in tags), "reversible")})
    return out[::-1][:limit]


def self_running(state):
    """T-2364: the Self-running page's figures: fm triggers --json (the table, recoveries, trials, installs waiting,
    the prediction book) and each generation's lean start (genesis-lean.json in the project's state); {} on failure."""
    try:
        p = subprocess.run([os.path.join(ROOT, "plugin", "bin", "fm"), "triggers", "--json"], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
        d = json.loads(p.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    sr = d.get("self_running") or {}
    verdicts = {}
    for t in d.get("trials") or []:
        v = t.get("verdict") or "due"
        verdicts[v] = verdicts.get(v, 0) + 1
    try:
        with open(os.path.join(state, "genesis-lean.json"), encoding="utf-8") as f:
            lean = json.load(f)
    except (OSError, ValueError):
        lean = {}
    return {"triggers": [{k: r.get(k) for k in ("trigger", "fired", "precision", "route")} for r in d.get("triggers") or []],
            "installs": sr.get("installs", 0), "recoveries": len(d.get("recoveries") or []), "trials": verdicts,
            "predictions": [sr["predictions"]] if sr.get("predictions") else [],
            "lean": [dict(gen=int(g), **(r.get("metrics") or {})) for g, r in sorted(lean.items(), key=lambda kv: int(kv[0]))
                     if str(g).isdigit() and isinstance(r, dict)]}


def proof(old):
    """T-2395: the verifying page's figures from fm prove --rollup --json, today's count added to the kept history (60 days)."""
    try:
        p = subprocess.run([os.path.join(ROOT, "plugin", "bin", "fm"), "prove", "--rollup", "--json"], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
        d = json.loads(p.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return old or {}
    if "count" not in d:
        return old or {}
    day = datetime.date.today().isoformat()
    hist = [h for h in (old or {}).get("history") or [] if h.get("day") != day] + [{"day": day, "n": d["count"]}]
    return {"n": d["count"], "counts": d.get("counts") or {}, "history": hist[-60:]}


def bench():
    p = subprocess.run([sys.executable, os.path.join(ROOT, "plugin", "tests", "bench_hooks.py"), "--runs", "20", "--json"],
                       capture_output=True, text=True, timeout=600)
    data = json.loads(p.stdout or "{}")
    return {k: {"p50": v["p50"], "p95": v["p95"], "budget": v["budget"]} for k, v in data.items() if "p95" in v}


def models(state):
    """T-2489: the Model layer page's figures: the newest exam line carrying fmexam.replay_models' per-use scores
    (research/genesis/exam.jsonl), as {run, replay}; {} when no run has scored one yet."""
    try:
        with open(os.path.join(state, "research", "genesis", "exam.jsonl"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("model_replay"), dict):
            return {"run": str(row.get("run") or ""), "replay": row["model_replay"]}
    return {}


def genesis(state, name):
    """A tower record's text (research/genesis/NAME), or None when there is none."""
    try:
        with open(os.path.join(state, "research", "genesis", name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def short(text, cap):
    """T-2767: a published line from a tower record: its first clause, no path and no quoted command, at most CAP
    characters."""
    text = re.split(r"\. |;| \(", text, maxsplit=1)[0]
    text = " ".join(re.sub(r"`[^`]*`|\S*/\S*", " ", text).split()).rstrip(" ,:")
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def louhi(state):
    """T-2767: Louhi's scorecard from the exam's trials (research/genesis/exam.jsonl), one line per arm per run: each
    run's trials, scenarios and the faults keyed and found; each arm's totals and the Brier score of its forecasts;
    and, by generation, what the harness's own arm found of what was keyed (a run is re-sat in later generations, so
    a generation is the line's, not the run's). A figure that isn't a number counts 0; a line with no arm isn't a
    trial."""
    out = {"trials": 0, "runs": [], "agents": [], "gens": []}
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    runs, agents, gens = {}, {}, {}
    for raw in (genesis(state, "exam.jsonl") or "").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("agent"), str):
            continue
        out["trials"] += 1
        a = agents.setdefault(row["agent"], collections.Counter())
        a["trials"] += 1
        for k in ("scenarios", "keyed", "found", "false_alarms", "fixable", "fixed", "brier_n", "brier_sum"):
            a[k] += num(row.get(k))
        if isinstance(row.get("run"), str):
            r = runs.setdefault(row["run"], {"run": row["run"], "trials": 0, "scenarios": 0, "keyed": 0, "found": 0})
            r["trials"] += 1
            for k in ("scenarios", "keyed", "found"):
                r[k] += num(row.get(k))
        if row["agent"] in HARNESS and type(row.get("gen")) is int:
            g = gens.setdefault(row["gen"], {"gen": row["gen"], "trials": 0, "keyed": 0, "found": 0})
            g["trials"] += 1
            for k in ("keyed", "found"):
                g[k] += num(row.get(k))
    out["runs"], out["gens"] = list(runs.values()), [gens[g] for g in sorted(gens)]
    out["agents"] = [{"agent": name, **{k: a[k] for k in ("trials", "scenarios", "keyed", "found", "false_alarms",
                                                          "fixable", "fixed")},
                      "brier": round(a["brier_sum"] / a["brier_n"], 3) if a["brier_n"] else None}
                     for name, a in sorted(agents.items(), key=lambda x: (-x[1]["trials"], x[0]))]
    return out


def moons(state):
    """T-2767: the moonshot catalogue (research/genesis/moonshot.md): each moon's id, title, stage, the moons it needs,
    and its tier (the reviewer's rating when it has one, else the author's)."""
    sys.path.insert(0, os.path.join(ROOT, "plugin", "lib"))
    from sampo import moons as smoons
    text = genesis(state, smoons.FILE)
    if text is None:
        return []
    try:
        with open(os.path.join(state, smoons.STORE), encoding="utf-8") as f:
            rated = json.load(f)
    except (OSError, ValueError):
        rated = {}
    rated = rated if isinstance(rated, dict) else {}
    out = []
    for m in smoons.parse(text):
        r = rated.get(m["id"])
        tier = r.get("tier") if isinstance(r, dict) and r.get("tier") in smoons.TIERS else m["author"]
        out.append({"id": m["id"], "title": short(m["title"], 120), "tier": tier, "stage": m["stage"],
                    "needs": re.findall(r"\bM-\d+\b", m["f"].get("needs", ""))})
    return out


def underworld(state):
    """T-2767: the ideas waiting for a better day (research/genesis/not-yet.md, the Removed list left out): each one's
    name, its blocker's first clause, how many approaches were tried, how many times the blocker was re-checked, and
    the generation that added it."""
    out = []
    for sec in re.split(r"(?m)^## ", genesis(state, "not-yet.md") or "")[1:]:
        head, _, body = sec.partition("\n")
        if head.strip().lower() == "removed":
            continue
        field = lambda k: re.findall(rf"(?m)^- {k}:\s*(.*)$", body)
        tries = 0
        for line in field("tried"):  # approaches split on ';' outside brackets: '(T-1: a; b)' is one
            depth, part = 0, ""
            for ch in line + ";":
                depth += (ch in "([") - (ch in ")]")
                if ch == ";" and depth <= 0:
                    tries += bool(part.strip())
                    part = ""
                else:
                    part += ch
        added = re.search(r"\bgen-\d+\b", " ".join(field("added")))
        out.append({"idea": short(head.strip(), 120), "blocker": short(" ".join(field("blocker")[:1]), 160),
                    "tries": tries, "rechecks": len(field("retried")), "added": added.group(0) if added else ""})
    return out


def merge_duels(old, new):
    """Site review: the duels named now replace the saved ones of the same name; the others stay, in order."""
    fresh = {d["name"]: d for d in new}
    return [fresh.pop(d["name"], d) for d in old] + list(fresh.values())


def duel(state, model=None, kind=""):
    """The newest fm bench duel --vs none (with that model, if given): each case's pass, cost, turns and time for both
    arms (T-1222). `kind` (T-1242): "long", the newest run with forced compaction; "marathon", the newest whose
    Sampo arm went on past the asked work; "", the newest with neither."""
    res = os.path.join(state, "bench", "results")
    for path in sorted(glob.glob(os.path.join(res, "duel-none-foreman-*.json")), reverse=True):
        bare_path = os.path.join(res, "duel-none-bare-" + path.rsplit("duel-none-foreman-", 1)[1])
        with open(path, encoding="utf-8") as f:
            fm_ = json.load(f)
        found = "marathon" if fm_.get("marathon") else "long" if fm_.get("compact_at") else ""
        if os.path.isfile(bare_path) and (not model or fm_.get("model") == model) and found == kind:
            break
    else:
        return None
    with open(bare_path, encoding="utf-8") as f:
        bare = json.load(f)
    try:  # T-1231: a commit case's id is its sha; its title names the module the task adds
        with open(os.path.join(state, "bench", "cases.json"), encoding="utf-8") as f:
            known = {x["id"]: x for x in json.load(f)}
    except (OSError, ValueError, TypeError, KeyError):
        known = {}
    titles = {k: x.get("title") or "" for k, x in known.items()}

    def per_request(x):  # T-1242: from the failing hidden tests when the run predates its own per-request count
        case = known.get(x["id"]) or {}
        if x.get("requests") or not case.get("requests"):
            return x.get("requests")
        sys.path.insert(0, os.path.join(ROOT, "plugin", "lib"))
        from sampo import bench as sbench
        return sbench.per_request(case, x.get("failed") or [])
    pick = lambda x: {"pass": bool(x.get("pass")), "cost": round(x.get("cost_usd") or 0, 3), "score": x.get("score"),
                      "runs": x.get("runs") or 1, "passed": round((x.get("pass_rate") or 0) * (x.get("runs") or 1)),
                      "seconds": x.get("seconds") or 0, "turns": x.get("turns") or 0, "error": bool(x.get("error")),
                      "tests": x.get("tests"), "compactions": x.get("compactions"), "capped": x.get("capped"),
                      "requests": per_request(x),
                      "each": [{k: a.get(k) for k in ("tests", "compactions")} for a in x.get("attempts") or []],
                      "found": [t for t in ((x.get("diag") or {}).get("titles") or {}).values()
                                if t.endswith("(discovered)")]}
    b = {x["id"]: x for x in bare.get("cases", [])}

    def name(cid):  # kit.lru → "lru"; otherwise the title's first words
        t = titles.get(cid, "")
        mod = re.findall(r"\bkit\.(\w+)", t)
        return mod[0] if mod else (" ".join(re.sub(r"[:(].*", "", t).split()[:5]).rstrip(",;.") or cid)
    cases = [{"case": name(x["id"]), "id": x["id"], "foreman": pick(x), "bare": pick(b[x["id"]])}
             for x in fm_.get("cases", []) if x["id"] in b]  # T-1261: the id and label fm bench repro takes
    return {"model": fm_.get("model"), "at": (fm_.get("at") or "")[:10], "claude": fm_.get("claude"), "cases": cases,
            "label": os.path.basename(path)[:-len(".json")],
            **({"compact_at": fm_.get("compact_at"), "marathon": fm_.get("marathon")} if kind else {}),
            **({"history": long_history(res, model)} if kind == "long" else {})}


def long_history(res, model=None):
    """T-1246: every three-run long-session duel (forced compaction, no marathon): runs passing every hidden test on
    each side and the mean cost a session, oldest first. One-run smokes are left out, as the page's method says."""
    out = []
    for path in sorted(glob.glob(os.path.join(res, "duel-none-foreman-*.json"))):
        bare_path = os.path.join(res, "duel-none-bare-" + path.rsplit("duel-none-foreman-", 1)[1])
        try:
            with open(path, encoding="utf-8") as f:
                a = json.load(f)
            with open(bare_path, encoding="utf-8") as f:
                b = json.load(f)
        except (OSError, ValueError):
            continue
        if not a.get("compact_at") or a.get("marathon") or (model and a.get("model") != model) or not a.get("cases"):
            continue
        x, y = a["cases"][0], (b.get("cases") or [{}])[0]
        if (x.get("runs") or 1) < 3:
            continue
        ok = lambda c: sum(bool(t.get("pass")) for t in c.get("attempts") or [])
        out.append({"model": a.get("model"), "runs": x["runs"], "f_pass": ok(x), "b_pass": ok(y),
                    "f_cost": round(x.get("cost_usd") or 0, 4), "b_cost": round(y.get("cost_usd") or 0, 4)})
    return out


def code_counts():
    """T-1246: the Sampo Code page's figures, counted from the repo: the model client's tests and the endpoints
    fm code finds (local servers and keyed clouds)."""
    with open(os.path.join(ROOT, "plugin", "tests", "test_models.py"), encoding="utf-8") as f:
        tests = len(re.findall(r"(?m)^    def test_", f.read()))
    sys.path.insert(0, os.path.join(ROOT, "plugin", "lib"))
    from sampo import models as smodels
    return {"model_tests": tests, "endpoints": len(smodels.LOCAL) + len(smodels.CLOUD), "tests": test_count("HEAD")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state")
    ap.add_argument("--duel", action="append", metavar="NAME=DIR",
                    help="a Sampo project state folder whose newest duel to show, named (repeatable); default: this "
                         "repo's, as the quality duel")
    ap.add_argument("--no-bench", action="store_true", help="keep the hook timings already in stats.json")
    a = ap.parse_args()
    state = a.state or state_dir()
    if not state:
        sys.exit("no Sampo state for this repo (SAMPO_HOME/state/projects/foreman-*)")
    old = {}
    if os.path.isfile(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    stats = {"generated": datetime.date.today().isoformat(), **ledger(state, None), "releases": releases(),
             "cards": cards(), "code": code_counts(), "calibration": calibration(), "proof": proof(old.get("proof")), "self_running": self_running(state), "models": models(state), "decisions": decisions(state),
             "louhi": louhi(state), "moons": moons(state), "underworld": underworld(state),"bench": old.get("bench", {}) if a.no_bench else bench(),
             "duels": merge_duels(old.get("duels") or [], [dict(d, name=n) for n, d in ((x.partition("=")[0], duel(
                 *x.partition("=")[2].split("@", 1), kind=next((k for k in ("long", "marathon") if x.startswith(k)), "")))
                                                     for x in (a.duel or [f"quality={state}"])) if d])}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1)
        f.write("\n")
    print(f"wrote {OUT}: {stats['totals']['tasks_done']} tasks, {len(stats['daily'])} days, "
          f"{len(stats['releases'])} releases, {len(stats['bench'])} hook timings")


if __name__ == "__main__":
    main()
