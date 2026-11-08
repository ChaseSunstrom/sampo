"""Kalevala (T-3050, T-3081): one Rune tower per aspect, run together. Each tower is rune's --tower home
(research/genesis/towers/NAME: its own gen-0, docs and steers); Kalevala's own state, its manifest and its generations,
sits in research/genesis/kalevala. Moons, exam scenarios and the user's steers stay shared at the root."""
import collections
import concurrent.futures
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

from sampo import budget
from sampo import core as c
from sampo import moonscale
from sampo import rune

FIXED = ("problem solving", "memory", "efficiency")  # the aspects every project gets, open coverage or not


def home(p):
    return os.path.join(rune.root(p), "kalevala")


def _manifest(p):
    return os.path.join(home(p), "manifest.json")


def load(p):
    """The manifest. A damaged one raises (json's ValueError): start never overwrites it from scratch."""
    if not os.path.exists(_manifest(p)):
        return {"towers": {}}
    with open(_manifest(p), encoding="utf-8") as f:
        return json.load(f)


def _save(p, m):
    os.makedirs(home(p), exist_ok=True)
    c.write_atomic(_manifest(p), json.dumps(m, indent=2, sort_keys=True) + "\n")


def tower_name(aspect):
    """A tower name from an aspect: lowercase words joined by '-', at most 24 characters (rune.TOWER_NAME), a letter
    first, never 'root'."""
    s = re.sub(r"[^a-z0-9]+", "-", aspect.lower()).strip("-")[:24].rstrip("-")
    if not rune._valid(s):
        s = ("aspect-" + s).strip("-")[:24].rstrip("-")
    return s


def aspects(p):
    """[(aspect, name)]: each coverage.md area with open coverage (an empty cell or an idea), then the fixed three.
    The first aspect to a name keeps it; a later one with the same name is left out."""
    cov = rune.coverage(p, rune._doc(rune.root(p), "coverage.md"))
    names = [r["area"] for r in cov["rows"] if r["empty"] or r["ideas"]]
    out, taken = [], set()
    for aspect in names + list(FIXED):
        name = tower_name(aspect)
        if name not in taken:
            taken.add(name)
            out.append((aspect, name))
    return out


def tower_rows(p):
    """Each opened tower: its aspect, whether its gen-0 still matches its seal, and its generation and verdicts."""
    info = {r["name"]: r for r in rune.towers(p)}
    out = []
    for name, t in sorted(load(p)["towers"].items()):
        if not rune._valid(name):  # a hand-edited name: skipped, as rune.towers skips such dirs (T-3266)
            continue
        rune.set_tower(name)
        try:
            sealed = rune.sealed(p) == ""
        finally:
            rune.set_tower(None)
        r = info.get(name, {})
        out.append({"name": name, "aspect": t["aspect"], "sealed": sealed, "generation": r.get("generation"),
                    "select": r.get("select"), "drift": r.get("drift"), "paused": t.get("paused"),
                    "restarted": t.get("restarted")})
    return out


def start(p):
    """Open a tower for each aspect that has none yet: its gen-0 is the root's sealed gen-0 plus the aspect."""
    bad = rune.sealed(p)
    if bad:  # the root's gen-0 comes first; every tower's gen-0 extends it
        raise ValueError(f"the root gen-0 isn't sealed ({bad}): fm genesis init --file GEN0 first")
    with open(os.path.join(rune.root(p), "gen-0.md"), "rb") as f:
        root = f.read()
    opened, skipped = [], []
    for aspect, name in aspects(p):
        if name in load(p)["towers"]:
            skipped.append(name)
            continue
        gen0 = root + f"\n## Aspect\n{aspect}: this tower's focus; its generations work this aspect's gaps first.\n".encode()
        rune.set_tower(name)  # rune.init takes its own lock, so it runs outside ours
        try:
            rune.init(p, gen0)
        finally:
            rune.set_tower(None)
        with c.lock(p.dir):
            m = load(p)
            m["towers"][name] = {"aspect": aspect}
            _save(p, m)
        opened.append(name)
    return {"towers": tower_rows(p), "opened": opened, "skipped": skipped}


FEATURE = "kalevala"  # Kalevala's own budget line: each tower step is charged here, its share of the day cap
EST_JOB = 0.25  # USD per tower step until the feature has its own recent runs (budget.estimate)
MAX_STEPS = 200  # the manifest's step log keeps the newest this many
JOB_TIMEOUT = 3 * 3600  # one genesis step, as night.py's jobs
JOB_VERBS = {"spec": False, "seed": True, "select": True}  # the stages run as jobs; True: the CLI takes a generation


class Busy(RuntimeError):
    """A tower's step is already running: its lock is held."""


@contextlib.contextmanager
def tower_lock(p, name):
    """One step per tower at a time: a non-blocking flock on the tower's lock file under Kalevala's home."""
    os.makedirs(home(p), exist_ok=True)
    with open(os.path.join(home(p), f"{name}.lock"), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy(name) from None
        yield


DECIDE_WAIT = 30.0  # seconds a step waits for another step's decision on one tower before it skips that tower as busy


@contextlib.contextmanager
def _decide_lock(p, name):
    """One tower's decide-and-act at a time: a flock on its own file, polled to a deadline as c.lock does, apart from
    the project lock (the acts take that) and from tower_lock (held for a job's hours). Other towers don't wait on
    it. Raises Busy past DECIDE_WAIT, or when the filesystem refuses the lock (ENOLCK)."""
    os.makedirs(home(p), exist_ok=True)
    deadline = time.monotonic() + DECIDE_WAIT
    with open(os.path.join(home(p), f"{name}.decide.lock"), "a") as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy(name) from None
                time.sleep(0.02)
            except OSError:  # ENOLCK on NFS: no lock to be had
                raise Busy(name) from None
        yield


def order(rows):
    """Stalled first (its restart goes first), then thinnest first (the lowest generation, then the name), then the
    stuck: a tower whose exam hasn't moved in two generations is last, so its slot goes to one that improves."""
    def rank(r):
        return (0 if r.get("state") == "stalled" else 2 if r.get("stuck") else 1, r.get("generation") or 0, r["name"])
    return [r["name"] for r in sorted(rows, key=rank)]


def job_argv(name, st):
    """The genesis job for a tower's next step, or None when its command isn't one a job runs (gen-1's seed is a put
    the main thread makes). The command names the generation: seed N writes gen-N/seed.md, select N judges gen N."""
    tok = st["command"].split()
    if len(tok) < 3 or tok[1] != "genesis" or tok[2] not in JOB_VERBS:  # rune's commands: the CLI name, then genesis
        return None
    argv = [sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "genesis", tok[2]]
    if JOB_VERBS[tok[2]]:
        if len(tok) < 4 or not tok[3].isdigit():
            return None
        argv.append(tok[3])
    return argv + ["--tower", name]


def plan(names, slots, fit):
    """(run now, queued, K): the first `fit` towers run, the rest wait; at most `slots` run at once."""
    if names and fit < 1:
        raise budget.BudgetError("kalevala: today's share of the budget is spent, no tower step fits; "
                                 "or raise it: fm budget set --day N --because \"<why>\"")
    run = names[:max(fit, 0)]
    return run, names[len(run):], min(max(slots, 1), len(run))


def share():
    """Kalevala's share of the day cap: half of it (of the cap as the economy leaves it), for all its towers' steps."""
    return budget.effective()[0]["day"] * 0.5


def spent_today():
    return round(sum(float(e.get("usd") or 0) for e in budget.ledger() if e.get("feature") == FEATURE), 4)


def _slots(p):
    prof = moonscale._local(bool(c.read_meta(p).get("sensitive")))
    return max(int(prof.get("slots") or 1), 1) if prof else 1


def _stage(p, name):
    """The tower's next action (rune.step, read-only), read with the tower's home in place."""
    rune.set_tower(name)
    try:
        return rune.step(p)
    finally:
        rune.set_tower(None)


def _run(argv, cwd, env):
    """(exit code, a failure's last output lines): T-3841, 26 towers failed with only 'rc 1' kept and nobody saw why."""
    r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=JOB_TIMEOUT)
    tail = [ln for x in (r.stdout, r.stderr) for ln in (x or "").strip().splitlines()[-2:]]  # a reason on either stream
    return r.returncode, "" if r.returncode == 0 else " | ".join(tail)


def _held(p, name):
    try:
        with tower_lock(p, name):
            return False
    except Busy:
        return True


def _headroom(p, runnable, k):
    """(USD per step, run now, queued, K): the least that Kalevala's share, the day's cap and this command's cap leave."""
    est = budget.estimate(FEATURE, 1, EST_JOB)
    cur, _ = budget.effective(p)
    left = min(share() - spent_today(), cur["day"] - budget.spent()[0], cur["run"] - budget.RUN["usd"])
    return (est,) + plan(runnable, k or _slots(p), int(left // est) if left > 0 else 0)


def _job(p, name, st, argv, env, runner):
    at = c.now()
    try:
        got = runner(argv, p.root, env)  # an exit code, or (exit code, why)
        rc, why = got if isinstance(got, tuple) else (got, "")
    except Exception as e:  # a timeout or a missing interpreter fails this tower's step; the others go on
        rc, why = -1, f"{type(e).__name__}: {e}"
    row = {"tower": name, "stage": st["stage"], "status": "ran" if rc == 0 else "failed", "rc": rc, "at": at}
    return dict(row, why=c.fit(c.plain(why), 300)) if rc and why else row


def step(p, k=None, runner=None, dry_run=False):
    """Each opened tower's next genesis step as a job: at most K at once, one step per tower at a time, inside
    Kalevala's share of the day's budget. Towers past the share wait, thinnest first. The round goes in the manifest."""
    halt = load(p).get("halted")  # the user's pause: no round starts until fm kalevala resume
    if halt:
        return {"halted": halt, "dry_run": dry_run, "k": 0, "est": 0.0, "share": round(share(), 4),
                "spent": spent_today(), "results": [], "queued": [], "rounds": load(p).get("rounds", 0)}
    runner = runner or _run
    budget.check(FEATURE, EST_JOB)  # the day and per-command caps still hold for Kalevala
    env = dict(os.environ, FOREMAN_NO_BACKGROUND="1", FOREMAN_TOWER_READONLY_EXAM="1", FOREMAN_SPEND_AS=FEATURE)
    allrows = watch(p)["towers"]
    rows = [r for r in allrows if not r.get("error")]  # an unreadable tower sits out and is listed as skipped
    byrow = {r["name"]: r for r in rows}
    sat = {} if dry_run else _decide(p, rows)  # Väinämöinen's acts before any job; a dry run acts on nothing
    with contextlib.ExitStack() as held:  # a round holds the locks of the towers it charges until their jobs end
        with c.lock(p.dir):  # stages, plan and charges under one lock: no stale stage, no two steps on one headroom
            names = order(rows)
            stages = {n: _stage(p, n) for n in names}
            argvs = {n: job_argv(n, stages[n]) for n in names}
            runnable = [n for n in names if argvs[n] and stages[n]["kind"] == "ok" and n not in sat]
            at = c.now()
            skipped = [{"tower": n, "stage": stages[n]["stage"], "status": sat.get(n, "skipped"), "rc": None, "at": at}
                       for n in names if n not in runnable]
            skipped += [{"tower": r["name"], "stage": "?", "status": "unreadable", "rc": None, "at": at,
                         "error": r["error"]} for r in allrows if r.get("error")]
            est, run, queued, width = _headroom(p, runnable, k)
            slotted = [n for n in queued if byrow[n]["stuck"]]  # a stuck tower's slot goes to one that improves
            if dry_run:
                results = [{"tower": n, "stage": stages[n]["stage"], "status": "planned"} for n in run]
                return {"dry_run": True, "k": width, "est": est, "share": round(share(), 4), "spent": spent_today(),
                        "results": results + skipped, "queued": queued, "rounds": load(p).get("rounds", 0)}
            busy, charged = [], []
            for n in run:  # a tower another step holds is busy and uncharged; the rest are charged before their jobs start
                try:
                    held.enter_context(tower_lock(p, n))
                except Busy:
                    busy.append(n)
                    continue
                budget.record(FEATURE, usd=est, runs=1, project=p.slug, detail=f"{n} {stages[n]['stage']}")
                charged.append(n)
        for n in slotted:
            _act(p, "slot", n, "its exam hasn't moved in two generations: this round's slot goes to a tower that improves",
                 undo="none: it runs the round it has a slot again")
        done = [{"tower": n, "stage": stages[n]["stage"], "status": "busy", "rc": None, "at": at} for n in busy]
        try:
            if charged:
                with concurrent.futures.ThreadPoolExecutor(max_workers=width) as ex:
                    futs = [ex.submit(_job, p, n, stages[n], argvs[n], dict(env, FOREMAN_SPEND_TOWER=n), runner)
                            for n in charged]
                    done += [f.result() for f in futs]
        finally:  # each job's own spend is charged under FEATURE by its child; the estimate given back leaves the real cost
            for n in charged:
                budget.record(FEATURE, usd=-est, runs=0, project=p.slug, detail=f"settle {n}")
    done += skipped
    with c.lock(p.dir):
        m = load(p)
        m["rounds"] = m.get("rounds", 0) + 1
        m["steps"] = (m.get("steps", []) + done)[-MAX_STEPS:]
        _save(p, m)
    _all_failed(p, done)
    return {"dry_run": False, "k": width, "est": est, "share": round(share(), 4), "spent": spent_today(),
            "results": done, "queued": queued, "rounds": m["rounds"]}


def _all_failed(p, done):
    """T-3841: a round whose every job failed is work: one open FIX names the commonest reason (all 26 towers failed
    their first step on 2026-11-06 and nothing said so for two days)."""
    failed = [r for r in done if r["status"] == "failed"]
    if not failed or len(failed) < sum(r["status"] in ("ran", "failed") for r in done):
        return None
    why = collections.Counter(r.get("why") or f"exit {r['rc']}" for r in failed).most_common(1)[0][0]
    try:
        from sampo import eco
        return eco.capture_once(p, f"Kalevala: every tower's step failed in a round\n{len(failed)} tower job(s) failed"
                                   f" ({', '.join(sorted({r['stage'] for r in failed}))}); the commonest reason: {why}",
                                "FIX", "kalevala")[0]
    except Exception:  # noqa: BLE001 — a capture that fails never fails the round; the manifest still has each why
        return None


AGENT = "foreman"  # the exam lines Kalevala reads are the ones fm exam grade stamps for this agent


def _tower_scores(p, names):
    """{tower: closed train scenarios on its newest graded run, or None when it has none}: rune's own reading of
    each tower's exam lines (the train split; a scenario counts only if every graded line of the run closed it)."""
    out = {}
    for name in names:
        rune.set_tower(name)
        try:
            lines = [x for x in rune.exam_lines(p, AGENT) if x.get("split") != "holdout"]
        finally:
            rune.set_tower(None)
        if not lines:
            out[name] = None
            continue
        run = lines[-1].get("run")
        out[name] = len(rune.train_scenarios([x for x in lines if x.get("run") == run]))
    return out


CAPS_FILE = "capabilities.md"  # written into each set tower's home; rune._seed carries it as a seed section
CAPS_MAX = 30  # the newest this many go into a tower's seed; the registry keeps every one
BEHAVIOUR = ("ideas", "think", "review", "plan", "memory", "recall", "prompt", "skills")  # what a close can change
NOT_BEHAVIOUR = ("plugin/tests/", "docs/")


def behaviour(files):
    """The Sampo behaviour areas a closed task's files touch (T-3081 step 4), sorted; [] when none. A word of the path
    (split on non-alphanumerics) names an area, or its plural: plan.py counts, planet.py doesn't. ponytail: word match,
    not a path map; upgrade: an explicit path map once a word misleads."""
    out = set()
    for f in files:
        if not f.startswith(NOT_BEHAVIOUR):
            words = set(re.split(r"[^a-z0-9]+", f.lower()))
            out.update(a for a in BEHAVIOUR if a in words or a + "s" in words)
    return sorted(out)


def registry(p):
    """Every closed task whose close changed Sampo's behaviour (its task_done row carries areas), oldest first, once."""
    seen, out = set(), []
    for e in c.ledger_events(p, ["task_done"]):
        d, task = e.get("data") or {}, e.get("task")
        if d.get("areas") and task and task not in seen:
            seen.add(task)
            out.append({"task": task, "title": d.get("title") or task, "areas": list(d["areas"]), "at": e.get("ts")})
    return out


def _caps_text(caps):
    """A tower's capabilities.md: the newest CAPS_MAX registry rows, one line each (titles folded to one line)."""
    return ("Sampo's closed work that changed how it behaves since gen-0, newest last. Use each one; never rebuild it.\n\n"
            + "".join(f"- {x['task']} ({' '.join(c.plain(x['title']).split())[:120]}): {', '.join(x['areas'])}\n"
                      for x in caps[-CAPS_MAX:]))


def _caps_path(p, name):
    return os.path.join(rune.root(p), "towers", name, CAPS_FILE)


def _gen_result(n, g):
    return {"gen": n, **{k: g[k] for k in ("verdict", "base", "reason", "scores")}, "helped": g.get("helped", {})}


def gen(p):
    """Kalevala's next generation: the tower set (every opened tower with a sealed gen-0) and each tower's setup, in
    gen-N/setup.md. The parent gen must be selected first, so each one builds on a judged one."""
    with c.lock(p.dir):
        m = load(p)
        done = m.get("gens", {})
        n = max((int(k) for k in done), default=0) + 1
        if n > 1 and "verdict" not in done[str(n - 1)]:
            raise ValueError(f"kalevala gen {n - 1} isn't selected yet: fm kalevala select {n - 1} first")
        rows = [t for t in tower_rows(p) if t["sealed"]]
        if not rows:
            raise ValueError("no sealed tower to set: fm kalevala start first")
        based = done[str(n - 1)]["base"] if n > 1 else "root gen-0"
        text = (f"# Setup\n\nGen: {n}\nBased on: {based}\n\nTowers this gen runs; select compares them with gen {n - 1} "
                f"on the towers both scored:\n" + "".join(f"- {t['name']}: {t['aspect']} (tower gen {t['generation']})\n"
                                                         for t in rows))
        caps = registry(p)  # step 4: every set tower's next seed carries the registry (rune._seed's section)
        ids = [x["task"] for x in caps]
        had = done[str(n - 1)].get("capabilities", []) if n > 1 else []
        if caps:
            for t in rows:
                c.write_atomic(_caps_path(p, t["name"]), _caps_text(caps))
        os.makedirs(os.path.join(home(p), f"gen-{n}"), exist_ok=True)
        c.write_atomic(os.path.join(home(p), f"gen-{n}", "setup.md"), text)
        done[str(n)] = {"set": [t["name"] for t in rows], "based_on": based, "at": c.now(), "capabilities": ids,
                        "new_capabilities": [i for i in ids if i not in had]}
        m["gens"] = done
        _save(p, m)
    return {"gen": n, "set": done[str(n)]["set"], "based_on": based,
            "new_capabilities": done[str(n)]["new_capabilities"]}


def select(p, n):
    """Judge Kalevala's gen n by the towers' combined exam results: the closed train scenarios summed over the towers
    both gens scored. Kept when that sum beats gen n-1's on them, reverted otherwise (the seed base then the parent's).
    Gen 1, or a gen sharing no scored tower with its parent, is the baseline. Writes gen-n/select.md and Kalevala's own
    lineage.md. A judged gen is returned as it stands."""
    with c.lock(p.dir):
        m = load(p)
        done = m.get("gens", {})
        g = done.get(str(n))
        if g is None:
            raise ValueError(f"no kalevala gen {n}: fm kalevala gen first")
        if "verdict" in g:
            return _gen_result(n, g)
        scores = _tower_scores(p, g["set"])
        if all(v is None for v in scores.values()):
            raise ValueError(f"no tower in gen {n} has a graded exam line: fm exam run RUN --agent {AGENT} in each "
                             f"tower, then fm kalevala select {n}")
        prev = done.get(str(n - 1)) if n > 1 else None
        common = [t for t in g["set"] if scores[t] is not None and prev and prev["scores"].get(t) is not None]
        if not common:
            verdict, base = "baseline", f"gen-{n}"
            why = f"no tower graded in gen {n - 1} as well: gen {n} is the baseline"
        else:
            now, before = sum(scores[t] for t in common), sum(prev["scores"][t] for t in common)
            verdict, base = ("kept", f"gen-{n}") if now > before else ("reverted", prev["base"])
            why = f"closed {now} train scenario(s) across {', '.join(common)}; gen {n - 1} closed {before} on them"
        # step 4: gen n-1's new capabilities went into the towers' seeds; did each tower that scored both gens rise?
        helped = {}
        if prev and prev.get("new_capabilities"):
            helped = {t: {"new": list(prev["new_capabilities"]), "rose": scores[t] > prev["scores"][t]} for t in common}
        g.update(scores=scores, verdict=verdict, base=base, reason=why, judged=c.now(), helped=helped)
        os.makedirs(os.path.join(home(p), f"gen-{n}"), exist_ok=True)
        c.write_atomic(os.path.join(home(p), f"gen-{n}", "select.md"),
                       f"# Select\n\nGen: {n}\nVerdict: {verdict}\nSeed base: {base}\nReason: {why}\n"
                       f"Scores: {json.dumps(scores, sort_keys=True)}\nHelped: {json.dumps(helped, sort_keys=True)}\n")
        lin = os.path.join(home(p), "lineage.md")
        old = "# Lineage\n"
        if os.path.exists(lin):
            with open(lin, encoding="utf-8") as f:
                old = f.read()
        if f"## gen-{n}\n" not in old:  # a judged gen's section is written once, even if a later save failed
            c.write_atomic(lin, old.rstrip("\n") + f"\n\n## gen-{n}\n- {verdict}: {why}\n- seed base: {base}\n")
        m["gens"] = done
        _save(p, m)
    return _gen_result(n, g)


STALL_HOURS = 24  # ponytail: one window for every tower; a window per aspect once one aspect needs its own pace


def _tower_runs(p, name):
    """The closed train scenarios of each graded run of a tower's exam, oldest run first (holdout never counts)."""
    rune.set_tower(name)
    try:
        lines = [x for x in rune.exam_lines(p, AGENT) if x.get("split") != "holdout"]
    finally:
        rune.set_tower(None)
    groups = {}  # one pass: run id -> its lines, in the order the runs first appear
    for x in lines:
        groups.setdefault(x.get("run"), []).append(x)
    return [len(rune.train_scenarios(v)) for v in groups.values()]


def _charged_to(detail):
    """The tower a Kalevala charge names: 'NAME STAGE' for a step, 'settle NAME' for its settlement, 'tower:NAME ...'
    for a tower's child job's own spend (budget tags it), '' for none."""
    t = str(detail or "").split()
    if t and t[0].startswith("tower:"):
        return t[0][len("tower:"):]
    if t and t[0] == "settle":
        return t[1] if len(t) > 1 else ""
    return t[0] if t else ""


def _tower_state(row, stall_h):
    """The first verdict that holds: unsealed, drifted, stalled, then the exam's (unscored, new, flat, improving, falling)."""
    if not row["sealed"]:
        return "unsealed"
    if row["drift"] == "drifted":
        return "drifted"
    if row["idle_h"] >= stall_h:
        return "stalled"
    if row["closed"] is None:
        return "unscored"
    if row["delta"] is None:
        return "new"
    return "improving" if row["delta"] > 0 else "falling" if row["delta"] < 0 else "flat"


def watch(p, now=None, stall_h=STALL_HOURS):
    """Each opened tower's state, read only (T-3082): its generation; the closed train scenarios on its newest graded
    run and the change from the run before; its drift verdict; hours since its newest generation (gen-0's file while it
    has none); spend today under Kalevala's line; and the Mission files of its newest generation. A tower whose files
    can't be read (T-3376) is a row of state "unreadable" with the short reason under "error"; the others still read."""
    now = time.time() if now is None else now
    ledger = budget.ledger()
    rows = []
    for t in tower_rows(p):
        try:
            rows.append(_watch_row(p, t, now, stall_h, ledger))
        except Exception as e:  # T-3376: one unreadable tower file shows as that tower's row, not a blank block
            rows.append({"name": t["name"], "aspect": t["aspect"], "sealed": t["sealed"], "generation": t["generation"],
                         "drift": t["drift"], "closed": None, "delta": None, "idle_h": 0.0, "spend": 0.0, "missions": 0,
                         "newest": "", "restarted": t.get("restarted"), "stuck": False, "state": "unreadable",
                         "error": c.fit(f"{type(e).__name__}: {e}", 80)})
    return {"now": round(now), "stall_hours": stall_h, "towers": rows}


def _watch_row(p, t, now, stall_h, ledger):
    name, runs = t["name"], _tower_runs(p, t["name"])
    rune.set_tower(name)
    try:
        tdir = rune.home(p)
        gens = sorted(int(m[1]) for x in (os.listdir(tdir) if os.path.isdir(tdir) else ())
                      if (m := rune.GEN.fullmatch(x)) and os.path.isdir(os.path.join(tdir, x)))
    finally:
        rune.set_tower(None)
    newest = os.path.join(tdir, f"gen-{gens[-1]}") if gens else os.path.join(tdir, "gen-0.md")
    idle = (now - os.path.getmtime(newest)) / 3600 if os.path.exists(newest) else 0.0
    missions = [x for x in os.listdir(newest) if x.startswith("mission-") and x.endswith(".md")] if gens else []
    row = {"name": name, "aspect": t["aspect"], "sealed": t["sealed"], "generation": t["generation"],
           "drift": t["drift"], "closed": runs[-1] if runs else None,
           "delta": runs[-1] - runs[-2] if len(runs) > 1 else None, "idle_h": round(idle, 1),
           "spend": round(sum(float(e.get("usd") or 0) for e in ledger if e.get("feature") == FEATURE
                              and _charged_to(e.get("detail")) == name), 4),
           "missions": len(missions), "newest": newest, "restarted": t.get("restarted"), "runs": runs,
           "stuck": len(runs) >= 3 and runs[-1] - runs[-2] <= 0 and runs[-2] - runs[-3] <= 0}
    row["state"] = _tower_state(row, stall_h)
    return row


def _watch_line(t):
    if t.get("error"):
        return f"{t['name']}: unreadable · {t['error']}"
    exam = "exam none" if t["closed"] is None else f"exam {t['closed']}" + (
        "" if t["delta"] is None else f" ({t['delta']:+d})")
    return (f"{t['name']}: {t['state']} · gen {t['generation']} · {exam} · drift {t['drift'] or 'none'} · "
            f"idle {t['idle_h']:g}h · ${t['spend']:.2f} today · {t['missions']} open Mission(s)")


ACTS_SHOWN = 20  # status lists the newest this many acts; the ledger keeps every one
FALLING_STEER = "Your exam fell since the last run: keep to gen-0's aspect and the user's steers before adding work."
PATHS = re.compile(r"[\w.-]+(?:/[\w.-]+)+")  # ponytail: a slash-joined word is a file; upgrade: the Mission's own file list


def _act(p, act, name, reason, undo, **extra):
    """One act in the ledger (T-3082): what Väinämöinen did to a tower, why, and the line that undoes it."""
    data = {"act": act, "tower": name, "reason": reason, "undo": undo, **extra}
    c.log_event(p, "kalevala_act", data=data)
    return data


def acts(p):
    """Every act logged, oldest first: each with its kind, tower, reason, undo line and time."""
    return [dict(e.get("data") or {}, at=e.get("ts")) for e in c.ledger_events(p, ["kalevala_act"])]


def _tower_set(p, name, **kv):
    with c.lock(p.dir):
        m = load(p)
        if name not in m["towers"]:
            raise ValueError(f"no kalevala tower {name!r}: fm kalevala start first")
        m["towers"][name].update(kv)
        _save(p, m)


def pause(p, reason, name=None):
    """Pause one tower (it sits out every round) or, with no name, the whole loop (no round starts) until resume."""
    if name:
        _tower_set(p, name, paused=reason)
    else:
        with c.lock(p.dir):
            m = load(p)
            m["halted"] = reason
            _save(p, m)
    return _act(p, "pause", name, reason, undo="fm kalevala resume" + (f" {name}" if name else ""))


def resume(p, name=None):
    """Clear one tower's pause, or with no name every pause and the loop's halt."""
    with c.lock(p.dir):
        m = load(p)
        if name and name not in m["towers"]:
            raise ValueError(f"no kalevala tower {name!r}: fm kalevala start first")
        for n in [name] if name else list(m["towers"]):
            m["towers"][n]["paused"] = None
        if not name:
            m.pop("halted", None)
        _save(p, m)
    return _act(p, "resume", name, "resumed", undo="fm kalevala pause" + (f" {name}" if name else ""))


def steer(p, name, text):
    """Add one steer line to a tower's steers.md (T-3082). The drift check comes first: a MUST: or NEVER: line would
    overrule gen-0's rules, so it's refused and logged. A line already written is not written again.
    ponytail: the check is the constraint syntax, not a reading of the steer against gen-0; upgrade: rune.drift on the
    steer's text."""
    line = " ".join(str(text).split())
    if rune._constraints(line):
        return _act(p, "steer_refused", name, "a steer can't carry a MUST: or NEVER: line: gen-0's rules stand",
                    undo="none: nothing was written") and None
    rune.set_tower(name)
    try:
        path = os.path.join(rune.home(p), "steers.md")
    finally:
        rune.set_tower(None)
    with c.lock(p.dir):
        old = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as h:
                old = h.read()
        if f"- {line}\n" in old:
            return None
        c.write_atomic(path, old + f"- {line}\n")
    return _act(p, "steer", name, line, undo=f"delete the line '- {line}' from {path}")


def _restart(p, r):
    """A stalled tower's restart (T-3082): its newest generation is copied into a snapshot first, and the copy is the
    undo. Once per newest generation: a mark stops a stalled tower from snapshotting every round."""
    name, newest = r["name"], r["newest"]
    if not os.path.exists(newest):
        return
    mark = os.path.getmtime(newest)
    if load(p)["towers"][name].get("restarted") == mark:
        return
    snap = os.path.join(home(p), "snapshots", f"{name}-{time.time_ns()}")
    base = os.path.basename(newest)
    os.makedirs(snap)
    if os.path.isdir(newest):
        shutil.copytree(newest, os.path.join(snap, base))
    else:
        shutil.copy2(newest, os.path.join(snap, base))
    _tower_set(p, name, restarted=mark)
    _act(p, "restart", name, f"no new generation in {r['idle_h']:g}h: restarted, its next step runs first",
         undo=f"mv {newest} {newest}.replaced && cp -r {os.path.join(snap, base)} {newest}", snapshot=snap)


def _touches(row):
    """The files a tower's newest Mission names: slash-joined words in its mission-*.md files (64 KB read each)."""
    out = set()
    if row["missions"] and os.path.isdir(row["newest"]):
        for f in sorted(os.listdir(row["newest"])):
            if f.startswith("mission-") and f.endswith(".md"):
                with open(os.path.join(row["newest"], f), encoding="utf-8", errors="replace") as h:
                    out.update(PATHS.findall(h.read(65536)))
    return out


def _decide(p, rows):
    """Väinämöinen's acts before a round's jobs (T-3082): a drifted tower is paused; a stalled one is snapshotted and
    restarted; a falling one gets a steer; of two towers on the same files the later one waits while it improves and
    merges (pauses, pointing at the other) when it doesn't. Returns {tower: status} for those that sit out this round."""
    out, seen = {}, []  # seen: (tower, files) of the towers that may run, ranked first
    byname = {r["name"]: r for r in rows}
    for n in order(rows):
        r = byname[n]
        with contextlib.ExitStack() as es:  # one tower's decision at a time: the state is re-read inside, so a second step sees the first's act
            try:
                es.enter_context(_decide_lock(p, n))
            except Busy:  # another step has held it past DECIDE_WAIT: this tower sits out the round
                out[n] = "busy"
                continue
            if load(p)["towers"][n].get("paused"):
                out[n] = "paused"
                continue
            if r["state"] == "drifted":
                pause(p, f"drift: off gen-0's rules: the cold read drifted ({r['drift']})", n)
                out[n] = "paused"
                continue
            if r["state"] == "stalled":
                _restart(p, r)
            if r["delta"] is not None and r["delta"] < 0:
                steer(p, n, FALLING_STEER)
            files = _touches(r)
            clash = next(((e, files & ef) for e, ef in seen if files & ef), None)
            if clash:
                e, shared = clash
                names = ", ".join(sorted(shared))
                if r["state"] == "improving":
                    _act(p, "wait", n, f"shares {names} with {e}, which ranks first: it waits this round",
                         undo="none: it runs the next round")
                    out[n] = "waiting"
                else:
                    _tower_set(p, n, paused=f"merged into {e}: both write {names}")
                    _act(p, "merge", n, f"merged into {e}: both write {names}; paused, not deleted",
                         undo=f"fm kalevala resume {n}")
                    out[n] = "merged"
                continue
            seen.append((n, files))
    return out


DRIFT_STOP = "drift stop: every tower is off gen-0's rules and paused for it; fm kalevala resume after a steer or a fix"


def loop(p, runner=None, stop=None, rounds=None):
    """Kalevala's rounds, one after another (T-3082 step 3): each round is a watch and then a step, and nothing ends the
    loop on its own. Halts: the user's pause (fm kalevala pause, read by step); Väinämöinen's drift stop (every tower
    paused and at least one for drift); stop(), the caller's limit (night's spend, usage or panic) checked before each
    round; idle (a round runs no job: nothing changes until a tower does); the breaker (serve.FAILS_MAX rounds whose
    jobs all failed). A budget cap raises budget.BudgetError out of step. rounds bounds a test only."""
    from sampo import serve
    n, fails = 0, 0
    while rounds is None or n < rounds:
        why = stop() if stop else None
        if why:
            return {"halted": why, "rounds": n}
        r = step(p, runner=runner)
        n += 1
        if r.get("halted"):
            return {"halted": r["halted"], "rounds": n}
        towers = load(p)["towers"]
        if towers and all(t.get("paused") for t in towers.values()) \
                and any(str(t.get("paused")).startswith("drift:") for t in towers.values()):
            pause(p, DRIFT_STOP)
            return {"halted": DRIFT_STOP, "rounds": n}
        ran = [x for x in r["results"] if x["status"] == "ran"]
        failed = [x for x in r["results"] if x["status"] == "failed"]
        fails = fails + 1 if failed and not ran else 0
        if fails >= serve.FAILS_MAX:
            return {"halted": f"{fails} rounds failed in a row", "rounds": n}
        if not ran and not failed:
            why = "; ".join(f"{x['tower']}: {x['error']}" for x in r["results"] if x.get("error"))
            return {"idle": "no tower can run a job this round" + (f" ({why})" if why else ""), "rounds": n}
    return {"rounds": n}


def cmd_kalevala(args):
    from sampo import cli as scli
    p = scli.resolve(args)
    action = args.action or "status"
    if action == "start":
        r = start(p)
        return scli.out(args, r, f"opened {len(r['opened'])} tower(s), {len(r['skipped'])} already open"
                                 + "".join(f"\n  {t['name']}: {t['aspect']}" for t in r["towers"]))
    if action == "status":
        m = load(p)
        shown = acts(p)[-ACTS_SHOWN:]
        r = {"halted": m.get("halted"), "towers": tower_rows(p), "acts": shown}
        lines = [f"halted: {m['halted']} (fm kalevala resume)"] if m.get("halted") else []
        last = {s.get("tower"): s for s in m.get("steps") or ()}  # T-3841: each tower's newest step, and why it failed
        lines += [f"{t['name']}: {t['aspect']} (sealed: {t['sealed']})"
                  + (f" PAUSED: {t['paused']}" if t.get("paused") else "")
                  + (f" · last step: {s['stage']} {s['status']}" + (f": {s['why']}" if s.get("why") else "")
                     if (s := last.get(t["name"])) else "") for t in r["towers"]]
        lines += [f"{a['act']} {a['tower'] or ''}: {a['reason']}\n    undo: {a['undo']}" for a in shown]
        return scli.out(args, r, "\n".join(lines) or "no towers yet: sampo kalevala start")
    if action == "pause":
        name = args.tower
        if name is not None and name not in load(p)["towers"]:
            raise scli.UsageError(f"kalevala pause: no tower {name!r}")
        r = pause(p, "paused by the user", name)
        return scli.out(args, r, f"paused {name or 'the loop'}: no round starts until fm kalevala resume")
    if action == "resume":
        name = args.tower
        if name is not None and name not in load(p)["towers"]:
            raise scli.UsageError(f"kalevala resume: no tower {name!r}")
        r = resume(p, name)
        return scli.out(args, r, f"resumed {name or 'every tower and the loop'}")
    if action == "step":
        r = step(p, k=args.k, dry_run=args.dry_run)
        head = (f"dry run: " if r["dry_run"] else "") + (f"{len(r['results'])} tower(s), at most {r['k']} at once; "
                f"Kalevala's share ${r['share']:.2f} today, ${r['spent']:.2f} spent")
        return scli.out(args, r, head + "".join(f"\n  {x['tower']}: {x['status']} ({x['stage']})" for x in r["results"])
                        + (f"\n  queued past the share, thinnest first: {', '.join(r['queued'])}" if r["queued"] else ""))
    if action == "watch":
        stall_h = STALL_HOURS if args.stall_hours is None else args.stall_hours
        if not stall_h > 0:  # also refuses nan, which would never stall a tower
            raise scli.UsageError("kalevala watch: --stall-hours takes a number of hours above 0")
        r = watch(p, stall_h=stall_h)
        return scli.out(args, r, "\n".join(_watch_line(t) for t in r["towers"]) or "no towers yet: sampo kalevala start")
    if action == "gen":
        r = gen(p)
        return scli.out(args, r, f"kalevala gen {r['gen']}: {len(r['set'])} tower(s), based on {r['based_on']}; "
                                 f"select it after its steps: fm kalevala select {r['gen']}")
    if action == "select":
        if args.n is None:
            raise scli.UsageError("kalevala select needs the generation: fm kalevala select N")
        r = select(p, args.n)
        return scli.out(args, r, f"kalevala gen {r['gen']}: {r['verdict']}, seed base {r['base']}; {r['reason']}")
    raise scli.UsageError(f"unknown kalevala action {action!r}")
