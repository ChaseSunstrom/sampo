"""T-5006 (slice of T-4314): the yardstick, how far a local model trails Claude, per kind of work.

Pure aggregation over what the three bench runners already stored, no run of its own:
  bench cases   <state>/projects/<slug>/bench/results/*.json  (kind: the case's `type` in that project's cases.json)
  SWE-bench     <state>/bench-swe/<run>/results.jsonl         (kind: the instance's repo)
  Terminal-Bench <state>/bench/tbench/<run>/results.json      (kind: the task's category)
rows() reads them into one shape {suite, run, project, id, kind, arm, model, harness, passed, excluded, at}; a kind is
the suite plus the kind inside it ("swe:django/django"), so two suites never share one.
  arm      from the model's profile: a local endpoint (loopback, a private LAN address, a .local or one-label host) is
           local, an anthropic-kind profile or a Claude Code alias (sonnet, opus, haiku, claude-*, none given) is
           claude, anything else is "other" and sits on neither side of the gap (it is counted, not dropped).
  harness  what ran the model: sampo, bare, or the stored setup when it isn't plain (ablate:X, extra:<rival plugin>,
           head:<sha> of a version duel, marathon:N, compact:N), so a duel's run never replaces plain Sampo's row.
  excluded "infra" when the run died of the harness, not the model (a SWE pull/docker failure or "harness error", a
           tbench build_failed/grade_failed): no verdict on the model, so it is counted apart and never a failure.
A task run again counts once per (task, arm, harness), by its newest run (the stored `at`, else the file's time), so a
case rerun ten times, or run on two local models, does not outweigh the rest. gap() compares only the tasks both arms
ran (same suite, project, id and harness): the claude pass rate minus the local one over those, with the tasks only
one arm ran, the excluded and the "other" ones as counts beside it. A pooled "all" sums the paired tasks. An unfiltered
gap reads the plain harnesses (sampo, bare); a harness named by the caller reads that one. table() prints it, a kind
under MIN_PAIRED paired tasks marked "low n" and sorted after the ones over it.
Models advise, proof decides: a gap is a reading, nothing here gates a close or a merge."""
import ipaddress
import json
import os
import re

from sampo import core
from sampo import models

MAX_ROWS = 200000  # rows read per store: a store that grew past this is cut, oldest files dropped first
MAX_JSON = 64 * 1024 * 1024  # bytes of one results file read; a bigger one is skipped
MAX_JSONL = 1024 * 1024 * 1024  # SWE's results.jsonl keeps each patch, so it may be large; read line by line
KIND_WIDTH = 80
MIN_PAIRED = 3  # paired tasks under which a kind's gap is one or two lucky tasks: table() marks it "low n"
PLAIN_HARNESSES = ("sampo", "bare")  # what an unfiltered gap reads; a duel, an ablation or a snippet arm is named
CLAUDE_NAMES = ("claude", "sonnet", "opus", "haiku", "anthropic")  # Claude Code's aliases and the anthropic profile
LOCAL_NAMES = tuple(n for n, _ in models.LOCAL)  # the built-in local servers' profile names
INFRA_SWE = ("pull", "docker")  # bench_swe's failure classes that say nothing about the model
INFRA_TBENCH = ("build_failed", "grade_failed")  # benchtb's `ended` values of a task the harness couldn't run or grade
_LAN4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))
_LAN6 = ipaddress.ip_network("fc00::/7")


def _local_host(host):
    """A host that is this machine or its own network: loopback, RFC 1918 / link-local / unique-local addresses, a
    .local name or a one-label name (resolved on the LAN). A name with dots elsewhere is a remote service."""
    if models.is_loopback(host):
        return True
    h = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h.endswith(".local") or "." not in h
    return ip.is_link_local or any(ip in n for n in (_LAN4 if ip.version == 4 else (_LAN6,)))


def arm_of(name, profiles=None):
    """"local", "claude" or "other" for a stored model name: its profile when `profiles` ({name: profile}) has one
    (a local host = local, kind anthropic = claude), else by the name as the bench stored it."""
    if name is None or name == "":
        return "claude"  # bench cases run Claude Code's own default model when no --model is given
    if not isinstance(name, str):
        return "other"
    prof = (profiles or {}).get(name)
    if isinstance(prof, dict):
        if prof.get("kind") == "anthropic":
            return "claude"
        host = ""
        try:
            from urllib.parse import urlsplit
            host = urlsplit(str(prof.get("base_url") or "")).hostname or ""
        except ValueError:
            pass
        return "local" if host and _local_host(host) else "other"
    low = name.lower()
    if low.startswith(CLAUDE_NAMES):
        return "claude"
    if low in LOCAL_NAMES or re.fullmatch(r"[\w.-]+:[\w.-]+", low):  # an Ollama tag: gemma3:12b
        return "local"
    return "other"


def _line(text, default="unknown"):
    """text on one printable line."""
    s = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    return " ".join(s.split()) or default


def _kind(suite, text):
    """suite:text on one printable line, cut to KIND_WIDTH."""
    return core.fit(f"{suite}:{_line(text)}", KIND_WIDTH)


def _read_json(path, cap=MAX_JSON):
    try:
        if os.path.getsize(path) > cap:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):
        return None


def _row(suite, run, project, id_, kind, model, harness, passed, at, profiles, excluded=""):
    return {"suite": suite, "run": run, "project": project, "id": id_, "kind": _kind(suite, kind),
            "arm": arm_of(model, profiles), "model": model, "harness": harness, "passed": bool(passed) and not excluded,
            "excluded": excluded, "at": at}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _newest_first(entries):
    """[(path, ...)] by the file's time, newest first, so a cap on the rows read drops the oldest."""
    return sorted(((_mtime(e[0]), e) for e in entries), key=lambda t: (t[0], t[1][0]), reverse=True)


def _case_harness(res):
    """The setup a bench result was run with, "sampo" when plain: bare (or the snippet arm), an ablation, a rival
    plugin beside Sampo, an old head's snapshot (a version duel), a marathon, a compaction threshold."""
    parts = []
    bare = res.get("bare")
    if bare:
        parts.append("bare" if bare is True else f"bare:{_line(bare)}")
    if res.get("ablate"):
        parts.append(f"ablate:{_line(res['ablate'])}")
    extra = res.get("extra")
    if extra:
        names = [os.path.basename(str(x).rstrip("/\\")) or str(x) for x in (extra if isinstance(extra, list) else [extra])]
        parts.append("extra:" + _line(",".join(names)))
    if res.get("head"):
        parts.append(f"head:{_line(str(res['head'])[:8])}")
    if res.get("marathon"):
        parts.append(f"marathon:{_line(res['marathon'])}")
    if res.get("compact_at"):
        parts.append(f"compact:{_line(res['compact_at'])}")
    if res.get("equal_spend"):
        parts.append("equal-spend")
    return "+".join(parts) or "sampo"


def _case_rows(state, profiles):
    base = os.path.join(state, "projects")
    files = []
    for slug in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        results = os.path.join(base, slug, "bench", "results")
        if os.path.isdir(results):
            files += [(os.path.join(results, fn), slug, fn) for fn in sorted(os.listdir(results)) if fn.endswith(".json")]
    types = {}
    for mtime, (path, slug, fn) in _newest_first(files):
        res = _read_json(path)
        if not isinstance(res, dict) or not isinstance(res.get("cases"), list):
            continue
        if slug not in types:
            types[slug] = {}
            cases = _read_json(os.path.join(base, slug, "bench", "cases.json"))
            for case in cases if isinstance(cases, list) else []:
                if isinstance(case, dict) and isinstance(case.get("id"), str):
                    types[slug][case["id"]] = case.get("type")
        stamp = core.parse_ts(res.get("at"))
        at = stamp.timestamp() if stamp else mtime
        harness = _case_harness(res)
        for x in res["cases"]:
            if isinstance(x, dict) and isinstance(x.get("id"), str):
                yield _row("cases", fn[:-5], slug, x["id"], types[slug].get(x["id"]), res.get("model"), harness,
                           x.get("pass"), at, profiles)


def _swe_infra(r):
    reason = r.get("reason")
    return "infra" if r.get("failure") in INFRA_SWE or (isinstance(reason, str) and reason.startswith("harness error")) \
        else ""


def _swe_rows(state, profiles):
    base = os.path.join(state, "bench-swe")
    runs = [(os.path.join(base, run, "results.jsonl"), run) for run in sorted(os.listdir(base))] if os.path.isdir(base) else []
    for at, (path, run) in _newest_first(runs):
        try:
            if os.path.getsize(path) > MAX_JSONL:
                continue
            f = open(path, encoding="utf-8")
        except OSError:
            continue
        with f:
            try:
                for line in f:
                    try:
                        r = json.loads(line)
                    except (ValueError, RecursionError):
                        continue  # a torn last line of a killed run
                    if isinstance(r, dict) and isinstance(r.get("instance_id"), str):
                        yield _row("swe", run, "", r["instance_id"], r.get("repo"), r.get("model"),
                                   _line(r.get("harness") or "sampo"), r.get("resolved"), at, profiles, _swe_infra(r))
            except (OSError, UnicodeDecodeError):
                continue


def _tbench_rows(state, profiles):
    base = os.path.join(state, "bench", "tbench")
    runs = [(os.path.join(base, run, "results.json"), run) for run in sorted(os.listdir(base))] if os.path.isdir(base) else []
    for at, (path, run) in _newest_first(runs):
        res = _read_json(path)
        if not isinstance(res, dict) or not isinstance(res.get("tasks"), list):
            continue
        for x in res["tasks"]:
            if isinstance(x, dict) and isinstance(x.get("id"), str):
                yield _row("tbench", run, "", x["id"], x.get("category"), res.get("model"),
                           _line(x.get("harness") or "sampo"), x.get("passed"), at, profiles,
                           "infra" if x.get("ended") in INFRA_TBENCH else "")


def rows(state_dir=None, profiles=None):
    """Every stored bench row, newest run per (suite, project, id, arm, harness), as one list of dicts. A model on
    neither side ("other") keeps its own row per model. An excluded run never replaces a real result. `profiles` is
    {name: profile}; None reads models.json (unreadable counts as none)."""
    state = state_dir or core.state_dir()
    if profiles is None:
        try:
            profiles = models.configured()[1]
        except (models.ModelError, OSError):
            profiles = {}
    newest = {}
    for source in (_case_rows, _swe_rows, _tbench_rows):
        try:
            for n, r in enumerate(source(state, profiles)):
                if n >= MAX_ROWS:
                    break
                key = (r["suite"], r["project"], r["id"], r["arm"], r["harness"], str(r["model"]) if r["arm"] == "other" else "")
                old = newest.get(key)
                if old is None or (not r["excluded"] if bool(r["excluded"]) != bool(old["excluded"])
                                   else (r["at"], r["run"]) >= (old["at"], old["run"])):
                    newest[key] = r  # a real result beats an excluded one, else the newer run wins
        except OSError:
            continue  # a store that can't be listed is one the yardstick lacks, not an error
    return sorted(newest.values(), key=lambda r: (r["suite"], r["kind"], r["id"], r["harness"], str(r["model"])))


COUNTS = ("claude_only", "local_only", "other", "excluded_claude", "excluded_local")


def _rates(paired, counts):
    """{claude, local, claude_n, ..., gap, paired, + COUNTS} from the paired (claude row, local row) pairs."""
    n = len(paired)
    cp, lp = sum(c["passed"] for c, _ in paired), sum(l["passed"] for _, l in paired)
    out = {"paired": n, "claude_n": n, "claude_pass": cp, "local_n": n, "local_pass": lp,
           "claude": cp / n if n else None, "local": lp / n if n else None, **counts}
    out["gap"] = out["claude"] - out["local"] if n else None
    return out


def gap(rs, harness=None):
    """{kind: {claude, local (pass rates over the paired tasks, None with none), claude_n, claude_pass, local_n,
    local_pass, paired, gap, claude_only, local_only, other, excluded_claude, excluded_local}}. gap is claude's pass
    rate minus local's over the tasks both arms ran (same suite, project, id, harness); claude_only/local_only count
    tasks one arm ran, other the rows of models on neither side, excluded_* the infrastructure failures (a task with
    one is left out of the pairing). "all" pools the paired tasks of every kind. `harness` keeps only that harness's
    rows; None keeps the plain ones (sampo, bare), so an ablation or a duel is read by naming it."""
    rs = [r for r in rs if (r["harness"] == harness if harness is not None else r["harness"] in PLAIN_HARNESSES)]
    by = {}
    for r in rs:
        by.setdefault(r["kind"], []).append(r)
    out, pairs = {}, {}
    for kind, group in sorted(by.items()):
        tasks, counts = {}, dict.fromkeys(COUNTS, 0)
        for r in group:
            if r["arm"] == "other":
                counts["other"] += 1
            else:
                tasks.setdefault((r["suite"], r["project"], r["id"], r["harness"]), {})[r["arm"]] = r
        paired = []
        for sides in tasks.values():
            bad = [a for a, r in sides.items() if r["excluded"]]
            for arm in bad:
                counts["excluded_" + arm] += 1
            if bad:
                continue
            if len(sides) == 2:
                paired.append((sides["claude"], sides["local"]))
            else:
                counts[next(iter(sides)) + "_only"] += 1
        out[kind] = _rates(paired, counts)
        pairs[kind] = paired
    if out:
        out["all"] = _rates([p for v in pairs.values() for p in v], {k: sum(g[k] for g in out.values()) for k in COUNTS})
    return out


def _cell(g, arm):
    return f"{g[arm + '_pass']}/{g[arm + '_n']}" if g[arm + "_n"] else "-"


def _note(g, min_paired):
    """What the cells leave out: a thin sample, the tasks one arm ran, the other models' rows, the infra failures."""
    unpaired, infra = g["claude_only"] + g["local_only"], g["excluded_claude"] + g["excluded_local"]
    parts = ["low n"] if g["paired"] and g["paired"] < min_paired else []
    parts += [f"{n} {word}" for n, word in ((unpaired, "unpaired"), (g["other"], "other-model"), (infra, "infra")) if n]
    return ", ".join(parts)


def table(g, min_paired=MIN_PAIRED):
    """The gap as text: one line per kind, a kind with a gap over `min_paired` paired tasks first (widest gap first),
    then the ones under it (marked "low n"), then the kinds without a gap, "all" last; a note says what the counts
    left out (unpaired, other-model and infrastructure-failed tasks)."""
    if not g:
        return "No bench rows from both a local and a claude model yet (sampo bench runs)."
    kinds = sorted((k for k in g if k != "all"),
                   key=lambda k: (g[k]["gap"] is None, g[k]["paired"] < min_paired, -(g[k]["gap"] or 0), k))
    order = kinds + (["all"] if "all" in g else [])
    width = max(len("kind"), *(len(k) for k in order))
    lines = [f"{'kind':<{width}}  {'claude':>8}  {'local':>8}  {'gap':>9}  note"]
    for k in order:
        x = g[k]
        pts = "n/a" if x["gap"] is None else f"{x['gap'] * 100:+.0f} pts"
        lines.append(f"{k:<{width}}  {_cell(x, 'claude'):>8}  {_cell(x, 'local'):>8}  {pts:>9}  {_note(x, min_paired)}".rstrip())
    return "\n".join(lines)
