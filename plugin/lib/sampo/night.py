"""fm night (T-0236): budgeted background work while the user is away — the daily second read of the last session,
the landscape scan when due, the sentinel (T-0888), one research-debt question, the court of the user's steers, a bench tournament for one
kept fm evolve candidate — each an existing fm command, kept while the night's estimate fits --max-usd and today's
remaining budget. Refused when usage is high, when usage is unknown the limit halves, a sensitive project runs
nothing, and the night stops as soon as real spend passes the limit or usage climbs (T-0298 review). Never merges;
the morning's fm digest says what ran. --tick (T-2356) is the model-free pass: orders, the cached CI result, and CI or issues only where
the user said yes. It runs from the Stop hook's sweep, and from the timers T-2357 stages: --stage writes the nightly and
hourly units into state and queues one plugin request; --install (the user's yes, through the guard) installs them."""
import fcntl
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time

from sampo import budget
from sampo import core as c
from sampo import draft
from sampo import serve

STALE_H = 12  # a statusline snapshot older than this says nothing about tonight's usage
JOB_TIMEOUT = 3 * 3600  # seconds one night job may take
_LEASE = None  # T-0921: the open lock file of a running night
NAMES = ("scout", "dream", "spar", "cold files", "guard cases", "steer checks", "compost", "consolidate", "sentinel", "moons grow", "moons funnel", "model bench", "next request", "smoke", "pre-plan", "model lesion", "second session", "landscape", "research debt", "court", "evolve candidate", "queue", "draft", "kalevala rounds")


GENERATORS = ("moons grow", "moons funnel")  # T-3296: the bulk CPU users; they take the project lock only briefly per row


def priority(name):
    """T-3296: a preexec_fn for one job's child. Generators run at nice 19. Not SCHED_IDLE: they do take the project lock
    (moonscale.add per row), and an idle-class holder gets ~0.3% of a saturated CPU, stretching a millisecond hold into
    seconds for every waiter. Every other job gets nice 10 at most (never lowering a parent already niced further)."""
    def apply():
        try:
            if name in GENERATORS:
                os.nice(19)
            else:
                os.nice(max(0, 10 - os.nice(0)))
        except (AttributeError, OSError):
            pass
    return apply


def current_model(p):
    """The model of the newest finished task here (task_done events carry it), or None."""
    return next(((e.get("data") or {}).get("model") for e in reversed(c.ledger_tail(p, 5000, kinds=("task_done",)))
                 if e.get("event") == "task_done" and (e.get("data") or {}).get("model")), None)


def next_lesion(p):
    """T-0734: the first rule or skill file not yet ablated (fm evolve --drop) on the current model, when this project
    has bench cases to gate it; None otherwise."""
    from sampo import bench
    model = current_model(p)
    if not model or not bench._load_cases(p):
        return None
    done = {(e.get("data") or {}).get("target") for e in c.ledger_tail(p, 20000, kinds=("evolve",)) if e.get("event") == "evolve"
            and (e.get("data") or {}).get("model") == model}
    root = os.path.dirname(c.PLUGIN_ROOT)
    files = [os.path.relpath(os.path.join(c.PLUGIN_ROOT, "rules", "foreman.md"), root)] + sorted(
        os.path.relpath(x, root) for x in glob.glob(os.path.join(c.PLUGIN_ROOT, "skills", "*", "SKILL.md")))
    return next((f for f in files if f not in done), None)


def jobs(p):
    """[(name, fm argv, USD estimate)] of what tonight could do, cheapest and most useful first."""
    from sampo import bench
    from sampo import outside
    meta, out = c.read_meta(p), [("scout", ["scout", "--slow"], 0.0)]  # T-0950: free to look: work nobody asked for
    out.append(("dream", ["dream"], 0.0))  # T-0665: free: the day's failures as tripwire candidates
    out.append(("spar", ["spar", "--capture", "--budget", "120"], 0.0))  # T-3281: free: hook fuzz, idle priority, crashes filed as FIX
    out.append(("cold files", ["map", "--cold", "--capture"], 0.0))  # T-0660: free: hot files nobody has read
    from sampo import replay
    if os.path.realpath(p.root) == os.path.realpath(replay.repo_root()) and replay.load_cases()[0]:
        out.append(("guard cases", ["replay", "--cases", "--capture"], 0.0))  # T-1162: free: only Foreman's own repo; a drift is one FIX
    if c.steer_rules(p):  # T-2350: free: the user's rules' checks; a failing one is a FIX and a name in the report
        out.append(("steer checks", ["genesis", "steer"], 0.0))
    out.append(("compost", ["tidy", "--apply", "--lanes-only"], 0.0))  # T-1071: free: worktrees and branches of closed tasks
    out.append(("consolidate", ["recall", "--lessons", "--consolidate", "--capture", "--from-night"], 0.0))  # T-1071: free: a merge proposal, retires nothing
    if any(e.get("event") == "task_done" for e in c.ledger_tail(p, 5000, kinds=("task_done",))):  # T-0888: free: closed tasks' checks on HEAD
        out.append(("sentinel", ["sentinel", "--bisect"], 0.0))
    from sampo import moonscale
    ready = moonscale.night_ready(p)  # T-2916: free: a local model answers; grow under the target, funnel while rows wait
    if "grow" in ready:
        out.append(("moons grow", ["moons", "grow", "--keep", str(moonscale.TARGET), "--max-calls", "250",
                                   "--local-only"], 0.0))
    if "funnel" in ready:
        out.append(("moons funnel", ["moons", "funnel", "--max-calls", "400", "--local-only"], 0.0))
    from sampo import louhi
    if not meta.get("sensitive") and moonscale._local(False) and louhi.bench_due(p):  # T-3093: free: once a Rune generation
        out.append(("model bench", ["exam", "models", "run", "--night"], 0.0))  # T-3487: its own exam lock; it runs beside a grow
    gap_run = None if meta.get("sensitive") or budget.degrade(p) else louhi.gap_pending(p)
    if gap_run:  # T-3930: Haiku on the held-out split a local profile answered, at most weekly (reused inside 7 days)
        out.append(("frontier gap", ["exam", "gap", gap_run, "--ref"], budget.estimate("exam-arm", 3, 0.5)))
    if not meta.get("sensitive") and moonscale._local(False):  # T-2912: free: the local model's next-request guess
        out.append(("next request", ["outcomes", "--predictions", "--ask-local"], 0.0))
    smoke = meta.get("smoke") or {}
    if any(k != "off" for k in smoke) and "off" not in smoke:  # T-0743: a target is set; the crawl runs locally
        out.append(("smoke", ["smoke"], 0.0))
    if meta.get("second_session") != c.now()[:10]:
        out.append(("second session", ["second", "session", "--if-due"], budget.estimate("second-session", 1, 0.05)))
    age = c.age_days(meta.get("landscape_at"))
    if age is None or age >= outside.LANDSCAPE_DAYS:
        out.append(("landscape", ["landscape", "--if-due"], budget.estimate("research", 4, 0.2)))
    lesion = None if budget.degrade(p) else next_lesion(p)
    if lesion:  # T-0734: a model nobody has ablated Foreman's rules and skills on yet
        out.append((f"model lesion {lesion}", ["evolve", "--target", lesion, "--drop"], budget.estimate("bench", 3, 0.5)))
    big = None if budget.degrade(p) else next(  # T-0730: optional, so it waits while usage runs ahead of pace
        (b for b in c.order_queue(c.load_briefs(p))[0] if b.tier == "L" and not b.section("Plan review").strip()), None)
    if big:  # a queued L task's plan read by another model while nobody is waiting on it
        out.append((f"pre-plan {big.id}", ["second", "plan", big.id], budget.estimate("second-plan", 1, 0.05)))
    events = c.ledger_tail(p, 5000)
    asked = {e.get("task") for e in events if e.get("event") == "research"}
    debt = next((b for b in c.rank_inbox(c.load_briefs(p)) if b.type == "RESEARCH" and b.id not in asked
                 and b.meta.get("source") != "cold"), None)  # local files are read here, never sent to web research
    if debt:  # the oldest research question nobody has asked yet: one a night
        out.append(("research debt", ["research", "ask", c.fit(debt.title, 300), "--task", debt.id],
                    budget.estimate("research", 4, 0.2)))
    if bench.court_cases(p, 3)[0]:
        est = budget.estimate("bench", 3, 0.5)
        out.append(("court", ["bench", "court", "--max", "3", "--budget", f"{max(0.1, est / 3):.2f}"], est))
    benched = {str((e.get("data") or {}).get("old", "")) for e in events if e.get("event") == "bench_versions"}
    cand = next((d.get("branch") for e in reversed(events) if e.get("event") == "evolve"
                 for d in [e.get("data") or {}] if d.get("kept") and d.get("branch")
                 and not any(str(d["branch"]) in b for b in benched)), None)
    if cand:  # a kept candidate nobody has raced yet against this Foreman
        est = budget.estimate("bench", 4, 0.5)
        out.append((f"evolve candidate {cand}", ["bench", "versions", cand, "--max", "2", "--budget",
                                                 f"{max(0.1, est / 4):.2f}"], est))
    briefs = c.load_briefs(p)
    state = {b.id: b.status for b in briefs}
    if any(b.tier in ("S", "M") and all(state.get(d) in c.CLOSED for d in c._deps(b))
           for b in c.order_queue(briefs)[0]):  # T-1046: an unblocked small task is waiting: fm run works it
        out.append(("queue", ["run", "--fit", "--max", "2", "--wait", "0"], budget.estimate("run", 2, 0.5)))
    top = draft._top(p)
    if top and not os.path.exists(os.path.join(p.dir, "draft.json")):  # T-1062: a small task drafted for review, never merged
        out.append((f"draft {top.id}", ["draft"], budget.estimate("run", 1, 0.3)))
    from sampo import kalevala
    if kalevala.load(p)["towers"]:  # T-3082: Kalevala's rounds run last, until the night's limit or a halt stops them
        out.append(("kalevala rounds", ["kalevala", "step"], 0.0))  # est 0: the night's own stop checks the limit each round
    return out


def _usage(p=None):
    """(why usage is high, or None; whether it is known): a snapshot older than STALE_H hours is not knowledge."""
    snaps = glob.glob(os.path.join(c.state_dir(), "sessions", "*.json"))
    fresh = any(time.time() - os.path.getmtime(s) < STALE_H * 3600 for s in snaps)
    return budget.usage_high(p), fresh


def units(p, max_usd=2.0):
    """T-0955, T-2357: {file: text}: the nightly fm night and hourly fm night --tick systemd user units (Persistent=, so
    a machine asleep at the hour runs it on waking), and crontab.txt, the same two lines for a machine without systemd."""
    fm = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.join(c.PLUGIN_ROOT, 'bin', 'fm'))}"
    # systemd and cron start with a bare environment: this state dir, and the tools on your PATH (claude, git)
    path = ":".join(d for d in os.environ.get("PATH", "").split(":") if d and not re.search(r"[\x00-\x1f\"%\s]", d))
    home, out, cron = c.foreman_home(), {}, []
    if re.search(r"[\x00-\x1f\x7f]", p.root + home + fm):  # a newline would add a line to the unit
        raise c.PolicyError("the project or Sampo home path holds a control character: no timer unit is written for it")
    sd = lambda s: s.replace("%", "%%")  # noqa: E731 - systemd expands %-specifiers in these fields
    for kind, every, when, args, minute in (("night", "nightly", "*-*-* 02:30", f"night --max-usd {max_usd:g}", "30 2"),
                                            ("tick", "hourly", "hourly", "night --tick", "0 *")):
        name = f"foreman-{kind}-{p.slug}"
        out[name + ".service"] = "\n".join([
            "[Unit]", f"Description=Sampo {kind} ({p.slug})", "", "[Service]", "Type=oneshot",
            f"WorkingDirectory={sd(p.root)}", f'Environment="FOREMAN_HOME={sd(home)}"', f'Environment="PATH={path}"',
            f"ExecStart={sd(fm)} {args}", ""])
        out[name + ".timer"] = "\n".join([
            "[Unit]", f"Description=Sampo {kind} ({p.slug}), {every}", "", "[Timer]", f"OnCalendar={when}",
            "Persistent=true", "", "[Install]", "WantedBy=timers.target", ""])
        cron.append(f"{minute} * * * cd {shlex.quote(p.root)} && FOREMAN_HOME={shlex.quote(home)} "
                    f"PATH={shlex.quote(path)} {fm} {args}")
    out["crontab.txt"] = "\n".join(cron) + "\n"
    return out


def _staged(p):
    return os.path.join(p.dir, "night", "staged")


def write_staged(p, issues=(), ci=False, max_usd=2.0):
    """T-2357: the units and plan.json in state only (nothing outside it); returns (plan, the lines the ask names)."""
    import hashlib
    import shutil
    files = units(p, max_usd)
    plan = {"units": sorted(n for n in files if n.endswith((".service", ".timer"))), "max_usd": max_usd,
            "watch": {"ci": bool(ci), "issues": sorted(set(issues))}}
    plan["hash"] = hashlib.sha256(json.dumps([files, plan], sort_keys=True).encode()).hexdigest()[:8]
    d = _staged(p)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    for name, text in dict(files, **{"plan.json": json.dumps(plan, indent=1) + "\n"}).items():
        c.write_atomic(os.path.join(d, name), text)
    lines = [f"nightly fm night at 02:30 (at most ${max_usd:.2f})", "hourly fm night --tick (no tokens)"] \
        + [f"issue watch {r}" for r in plan["watch"]["issues"]] \
        + (["CI check with no session open"] if ci else []) + ["undo: fm night --uninstall"]
    return plan, lines


def _plan(p):
    try:
        with open(os.path.join(_staged(p), "plan.json"), encoding="utf-8") as f:
            plan = json.load(f)
        return plan if isinstance(plan, dict) and isinstance(plan.get("units"), list) else None
    except (OSError, ValueError):
        return None


def install(p):
    """T-2357: the staged units (staged now when none are) go to the systemd user folder and their timers start; the
    watch lines that were staged, and only those, are turned on. The guard lets an agent run this only after the user's
    yes to the queued plugin request; a user typing it has said yes."""
    import shutil
    plan = _plan(p) or write_staged(p)[0]
    if not shutil.which("systemctl"):
        raise c.PolicyError(f"no systemd --user here: the staged crontab lines are in "
                            f"{os.path.join(_staged(p), 'crontab.txt')} (crontab -e)")
    udir = serve.unit_dir()
    os.makedirs(udir, exist_ok=True)
    names = [n for n in plan["units"] if re.fullmatch(rf"foreman-(night|tick)-{re.escape(p.slug)}\.(service|timer)", n)]
    for n in names:
        shutil.copyfile(os.path.join(_staged(p), n), os.path.join(udir, n))
    serve._systemctl("daemon-reload")
    for n in names:
        if n.endswith(".timer"):
            serve._systemctl("enable", "--now", n)
    first, tick = "", f"foreman-tick-{p.slug}.service"
    if tick in names:  # T-2542: the yes is followed by one run in the timer's own environment, seen to work
        r = serve._systemctl("start", tick, check=False)
        try:
            res = {} if r.returncode else _show(tick, "Result", "ExecMainStatus")
        except (OSError, subprocess.SubprocessError):
            res = {}
        first = ("first tick ran: success" if res.get("Result") == "success" else
                 f"first tick failed: {c.plain((r.stderr or '').strip())[:160]} → journalctl --user -u {tick}"
                 if r.returncode else
                 f"first tick failed ({c.plain(res['Result'])[:40]}, exit {c.plain(res.get('ExecMainStatus') or '?')[:10]})"
                 f" → journalctl --user -u {tick}" if res.get("Result") else
                 "first tick started; systemd recorded no result")
    w = plan.get("watch") if isinstance(plan.get("watch"), dict) else {}
    watch = {"ci": w.get("ci") is True, "issues": [r for r in w.get("issues") or [] if isinstance(r, str) and _REPO.fullmatch(r)]}
    with c.lock(p.dir):
        meta = c.read_meta(p)
        meta["watch"] = watch
        meta["night_units"] = {"units": names, "home": c.foreman_home(), "at": c.now()}
        c.write_meta(p, meta)
    c.log_event(p, "night_installed", data={"units": names, "watch": watch, "first": first})
    shutil.rmtree(_staged(p), ignore_errors=True)
    return names, watch, first


def uninstall(p):
    """T-2357: the timers stopped and their units removed, the watch lines off."""
    meta = c.read_meta(p)
    names = (meta.get("night_units") or {}).get("units") or list(units(p))[:4]
    names = [n for n in names if re.fullmatch(rf"foreman-(night|tick)-{re.escape(p.slug)}\.(service|timer)", str(n))]
    for n in names:
        if n.endswith(".timer"):
            try:
                serve._systemctl("disable", "--now", n, check=False)
            except c.PolicyError:  # no systemd here: the files still go
                pass
        try:
            os.remove(os.path.join(serve.unit_dir(), n))
        except FileNotFoundError:
            pass
    try:
        serve._systemctl("daemon-reload", check=False)
    except c.PolicyError:
        pass
    with c.lock(p.dir):
        meta = c.read_meta(p)
        meta.pop("night_units", None)
        meta.pop("watch", None)
        c.write_meta(p, meta)
    c.log_event(p, "night_uninstalled", data={"units": names})
    return names


ISSUES_EVERY = 1800  # a repo's issues are polled at most this often (the Stop sweep ticks every 10 min)
ISSUES_LIMIT = 10  # new issues captured per poll (a first poll of a busy tracker is not 30 tasks)
_REPO = re.compile(r"[\w.-]+/[\w.-]+")


def _watch(meta):
    """(CI fetch approved, [repos whose issue watch is approved]): meta 'watch' {"ci": true, "issues": ["o/r"]}, which
    only T-2357's one-yes install writes after the user's yes. Anything else reads as no."""
    w = meta.get("watch") if isinstance(meta.get("watch"), dict) else {}
    issues = w.get("issues")
    return w.get("ci") is True, [r for r in issues if isinstance(r, str) and _REPO.fullmatch(r)] if isinstance(issues, list) else []


BLIND = "tick-blind.json"  # T-2607: what the newest tick could not watch, and when that list last changed


def _blind(meta, repo, ci_ok, repos):
    """T-2607: [[kind, why]] the tick could not watch this pass, so a quiet night means quiet."""
    if meta.get("sensitive"):
        return [[k, "the project is marked sensitive: no remote reads"] for k in ("CI", "issues")]
    if not repo:
        return []  # no GitHub repo: nothing to watch
    if os.environ.get("FOREMAN_NO_NET"):
        return [[k, "FOREMAN_NO_NET is set: no remote reads"] for k in ("CI", "issues")]
    out = [] if ci_ok else [["CI", "no watch yes for CI, the cached result only (fm night --stage --watch-ci, then your yes)"]]
    return out + ([] if repo in repos else
                  [["issues", f"no watch yes for {repo}'s issues (fm night --stage --watch-issues {repo}, then your yes)"]])


def blind_state(p):
    """{blind, changed} the newest tick left, or {}."""
    try:
        with open(os.path.join(p.dir, BLIND), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


CAP = 20  # issues the tick captures per repo per day: a flood of issues is not a flood of tasks


def fetches(p, days=7):
    """[(date, kind, repo, requests)] the tick sent off the machine in the last days, from its night_tick ledger
    events: routine approved polls are listed here (fm decide --review prints them), not as pending [outward] rows."""
    cut = (c.datetime.date.today() - c.datetime.timedelta(days=days)).isoformat()
    n = {}
    for e in c.ledger_tail(p, 5000, kinds=("night_tick",)):
        if e.get("event") == "night_tick" and str(e.get("ts"))[:10] >= cut:
            for f in (e.get("data") or {}).get("fetched") or []:
                k = (str(e["ts"])[:10], str(f.get("kind")), str(f.get("repo")))
                n[k] = n.get(k, 0) + 1
    return [k + (v,) for k, v in sorted(n.items())]


def _row(p, text, why):
    """One [outward] decision (fm todo holds it for the user's look), for something new: the first request for a repo,
    a failure, the cap. Once a day for the same words."""
    from sampo import cli
    if not any(d == c.now()[:10] and t == text for d, _, t in c._decision_rows(p)):
        cli._write_decision(p, text, why, "", "outward")


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


EVERY = {"tick": 3600, "night": 86400}  # T-2542: each installed timer's interval, in seconds


def _show(unit, *props):
    """systemd's own record of a user unit, {prop: value}; OSError when systemctl can't run."""
    r = subprocess.run(["systemctl", "--user", "--timestamp=unix", "show", unit, *(f"--property={x}" for x in props)],
                       capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise OSError(r.stderr.strip()[:200] or f"systemctl exited {r.returncode}")
    return dict(x.split("=", 1) for x in r.stdout.splitlines() if "=" in x)


def liveness(now=None, show=None):
    """T-2542: [(unit, why)] for each installed foreman-night/tick timer that stopped firing (no trigger in three
    intervals; a never-fired one is judged from its unit file's age) or whose last run failed, from systemd's record.
    One ('systemd', why) row when units are installed but systemctl can't be read. A quiet tick logs nothing, so this
    is the only way silence tells apart from a dead timer."""
    from sampo import serve
    show, now, out = show or _show, now or time.time(), []
    for path in sorted(glob.glob(os.path.join(serve.unit_dir(), "foreman-*-*.timer"))):
        name = os.path.basename(path)[:-len(".timer")]
        kind = name.split("-")[1]
        if kind not in EVERY:
            continue
        try:
            last = show(f"{name}.timer", "LastTriggerUSec").get("LastTriggerUSec", "")
            svc = show(f"{name}.service", "Result", "ExecMainStatus")
            since = float(last[1:]) if last.startswith("@") else os.path.getmtime(path)
        except (OSError, ValueError, subprocess.SubprocessError):
            return [("systemd", "timers are installed but systemctl --user can't be read, so Sampo can't tell "
                                "whether they fire")]
        if now - since > 3 * EVERY[kind]:
            out.append((name, f"hasn't fired in {int((now - since) // 3600)} h"
                              + ("" if last.startswith("@") else " (never, since it was installed)")))
        if svc.get("Result") not in (None, "", "success"):
            out.append((name, f"last run failed ({c.plain(svc['Result'])[:40]}, exit "
                              f"{c.plain(svc.get('ExecMainStatus') or '?')[:10]})"))
    return out


def tick(p, fetch_ci=None):
    """T-2356: the model-free pass, from the Stop hook's sweep and the timer: standing orders (file and schedule
    triggers), the CI result fmforesight cached, and new issues of a repo the user approved. Spends no tokens and runs
    no child. Remote reads only with the user's yes (meta watch), never when sensitive or FOREMAN_NO_NET; CI is fetched
    here only when approved and no session is open (fetch_ci=None decides; the sweep passes False: it fetched its own).
    {orders, captured, fetched: [{kind, repo}], notes}."""
    from sampo import cli
    from sampo import eco
    from sampo import foresight
    from sampo import orders
    if c.panicked():
        raise c.PolicyError(c.PAUSED)
    res = {"orders": [], "captured": [], "fetched": [], "notes": [], "settled": 0, "blind": []}
    lease = open(os.path.join(p.dir, "tick.lock"), "w")  # held until this returns (a second tick finds it taken)
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        return dict(res, notes=["another tick is running"])
    before, path, loud = None, os.path.join(p.dir, "tick.json"), []
    try:
        with lease:
            meta = c.read_meta(p)
            net = not os.environ.get("FOREMAN_NO_NET") and not meta.get("sensitive")
            ci_ok, repos = _watch(meta)
            before = {b.id for b in c.load_briefs(p, include_archive=True)}
            try:
                if orders.load(p):
                    res["orders"] = [o["n"] for o in orders.run(p)]
            except Exception as e:  # an order file never costs the tick
                res["notes"].append(f"standing orders: {c.fit(c.plain(str(e)), 120)}")
            try:  # T-2406: score the pending next-request prediction, make the next (local reads, never sensitive)
                foresight.prediction_tick(p)
            except Exception as e:  # the book never costs the tick
                res["notes"].append(f"predictions: {c.fit(c.plain(str(e)), 120)}")
            try:  # T-2407: the top prediction planned ahead while it beats its baseline (a plan, never code)
                draft.ahead(p)
            except Exception as e:
                res["notes"].append(f"draft ahead: {c.fit(c.plain(str(e)), 120)}")
            try:  # T-2408: dates about to pass, stale co-change partners, state nearing its budget (once a day)
                foresight.breakage_tick(p)
            except Exception as e:
                res["notes"].append(f"breakage warnings: {c.fit(c.plain(str(e)), 120)}")
            try:  # T-2488: the model layer's shadow pass: local backends only, a few rows a tick, nothing spent without one
                from sampo import models
                models.shadow_pass(p, cap=5)
            except Exception as e:
                res["notes"].append(f"shadow pass: {c.fit(c.plain(str(e)), 120)}")
            try:  # T-2499: bets settle on a night with no fm task finish (outcome rows written, the bar check run)
                from sampo import outcomes
                res["settled"] = outcomes.maintain(p) or 0  # T-2520: on record when nonzero
            except Exception as e:
                res["notes"].append(f"calibration: {c.fit(c.plain(str(e)), 120)}")
            try:
                with open(path, encoding="utf-8") as f:
                    seen = json.load(f)
            except (OSError, ValueError):
                seen = {}
            seen = seen if isinstance(seen, dict) else {}
            stamps = seen["issues"] if isinstance(seen.get("issues"), dict) else {}
            told = [x for x in seen["told"] if isinstance(x, str)] if isinstance(seen.get("told"), list) else []
            day = seen["day"] if isinstance(seen.get("day"), dict) else {}
            today = c.now()[:10]
            made = day["n"] if day.get("date") == today and isinstance(day.get("n"), dict) else {}

            def save():
                c.write_atomic(path, json.dumps({"issues": stamps, "told": told, "day": {"date": today, "n": made}}))

            def first(kind, repo, what):  # the first request of a kind for a repo is one outward row; later ones aren't
                if f"{kind} {repo}" not in told:
                    told.append(f"{kind} {repo}")
                    loud.append((f"night tick {what} {repo} for the first time", "your yes to the watch line; later "
                                 "requests are night_tick ledger events (fm decide --review lists them)"))

            repo = None if meta.get("sensitive") else foresight.github_repo(p.root)
            res["blind"] = _blind(meta, foresight.github_repo(p.root) if os.environ.get("FOREMAN_NO_NET") else repo,
                                  ci_ok, repos)
            if blind_state(p).get("blind") != res["blind"]:  # T-2607: the change is what the session hears about
                c.write_atomic(os.path.join(p.dir, BLIND), json.dumps({"blind": res["blind"], "changed": time.time()}))
            if repo:
                sent = []
                try:  # the cached result always; a fetch only with the yes, and with nobody at a keyboard to ask
                    from sampo import bus
                    ask = net and ci_ok and (not bus.live() if fetch_ci is None else fetch_ci)
                    foresight._ci(p, fetch=bool(ask), sent=sent)
                except Exception as e:
                    res["notes"].append(f"CI: {c.fit(c.plain(str(e)), 120)}")
                if sent:  # listed even when the capture after it failed
                    res["fetched"].append({"kind": "ci", "repo": repo})
                    first("ci", repo, "fetched the CI result of")
                    save()
            if net and repo and repo in repos and time.time() - _num(stamps.get(repo)) >= ISSUES_EVERY:
                try:
                    left = CAP - int(_num(made.get(repo)))
                except (OverflowError, ValueError):
                    left = 0
                if left <= 0:
                    res["notes"].append(f"issues of {repo}: daily cap of {CAP} captured, polling resumes tomorrow")
                else:
                    stamps[repo] = time.time()
                    first("issues", repo, "polled GitHub issues of")
                    save()  # stamped first: a failing gh waits its interval too
                    res["fetched"].append({"kind": "issues", "repo": repo})
                    try:  # fm inbox gh's path: plain, defanged, quoted behind "| "; an issue's asks are never run
                        got = eco.inbox_gh(p, repo, min(ISSUES_LIMIT, left))
                        made[repo] = int(_num(made.get(repo))) + len(got["captured"])
                        save()
                        if made[repo] >= CAP:
                            res["notes"].append(f"issues of {repo}: daily cap of {CAP} captured, polling resumes tomorrow")
                            loud.append((f"night tick stopped capturing issues of {repo} today: {CAP} captured (the cap)",
                                         "a flood of issues is not a flood of tasks; polling resumes tomorrow"))
                    except Exception as e:  # not logged in, no gh, offline: said, never fatal
                        why = c.fit(c.redact(c.plain(str(e))), 160)
                        res["notes"].append(f"issues of {repo}: {why}")
                        loud.append((f"night tick's poll of GitHub issues of {repo} failed", why))
    finally:  # what left the machine is listed even when something after it failed
        if before is not None:
            res["captured"] = sorted({b.id for b in c.load_briefs(p, include_archive=True)} - before)
        for text, why in loud:
            _row(p, text, why)
        if res["fetched"] or res["orders"] or res["captured"] or res["settled"]:
            c.log_event(p, "night_tick", data={k: res[k] for k in ("orders", "captured", "fetched", "settled")},
                        session=cli.session())
    return res


def _kalevala_rounds(p, start, limit):
    """T-3082: Kalevala's rounds (watch, then step) in this process until the night's spend limit, usage, panic or a
    halt stops them. The stop is checked before each round, so the limit holds to the round."""
    from sampo import kalevala

    def stop():
        if budget.spent()[0] - start >= limit:
            return f"spend reached the limit ${limit:.2f}"
        if budget.usage_high(p):
            return f"usage climbed ({budget.usage_high(p)})"
        return c.PAUSED if c.panicked() else None
    try:
        res = kalevala.loop(p, stop=stop)
    except budget.BudgetError as e:  # a day or run cap: the rounds stop, the night records why
        return {"name": "kalevala rounds", "exit": 1, "tail": c.fit(c.plain(str(e)), 160)}
    why = res.get("halted") or res.get("idle") or ""
    return {"name": "kalevala rounds", "exit": 1 if "failed in a row" in why else 0,
            "tail": c.fit(c.plain(f"{res['rounds']} round(s)" + (f"; stopped: {why}" if why else "")), 160)}


def cmd_night(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "stage", False):  # T-2357: staged in state, asked once (the install queue); nothing installed
        from sampo import grants
        issues = getattr(args, "watch_issues", None) or []
        bad = [r for r in issues if not _REPO.fullmatch(r)]
        if bad:
            raise cli.UsageError(f"--watch-issues takes OWNER/REPO, not {bad[0]!r}")
        b = c.find_brief(p, args.task) if getattr(args, "task", None) else c.active_brief_fast(p)
        if not b or b.status in c.CLOSED:
            raise cli.UsageError("fm night --stage queues its request on an open task: focus one or pass --task ID")
        plan, lines = write_staged(p, issues, getattr(args, "watch_ci", False), args.max_usd)
        state, _ = grants.queue(p, b.id, ["plugin"], "; ".join(lines), what=f"night timers {plan['hash']}",
                                  fallback="fm night --tick runs from the Stop hook's sweep while a session is open",
                                  session=cli.session())
        said = {"queued": f"queued on {b.id} for the user's yes (fm todo)", "dedupe": f"already queued on {b.id}",
                "refused": "the user said no to this before: not asked again", "missing": "no such task"}[state]
        return cli.out(args, {"staged": _staged(p), "plan": plan, "lines": lines, "task": b.id, "state": state},
                         "\n".join([f"night timers staged in {_staged(p)}; {said}:"] + [f"- {x}" for x in lines]
                                   + ["After the yes: fm night --install."]))
    if getattr(args, "install", False):
        names, watch, first = install(p)
        return cli.out(args, {"units": names, "watch": watch, "first": first},
                         f"installed and started: {', '.join(n for n in names if n.endswith('.timer'))}; CI check "
                         f"{'on' if watch['ci'] else 'off'}; issue watch: {', '.join(watch['issues']) or 'none'}. "
                         + (f"{first[0].upper()}{first[1:]}. " if first else "") + "Undo: fm night --uninstall")
    if getattr(args, "uninstall", False):
        names = uninstall(p)
        return cli.out(args, {"removed": names}, f"removed: {', '.join(names) or 'nothing installed'}; watch lines off")
    if getattr(args, "tick", False):
        res = tick(p)
        return cli.out(args, res, "tick: " + ", ".join(
            [f"{len(res['orders'])} order(s) fired", f"{len(res['captured'])} captured"]
            + ([f"{res['settled']} bet(s) settled"] if res.get("settled") else [])
            + [f"fetched {f['kind']} of {f['repo']}" for f in res["fetched"]] + res["notes"]))
    if c.panicked():
        raise c.PolicyError(c.PAUSED)
    if not getattr(args, "dry_run", False):  # T-0921: one night at a time (a cron firing while a manual one runs)
        global _LEASE
        _LEASE = open(os.path.join(p.dir, "night.lock"), "w")  # held until the process ends
        try:
            fcntl.flock(_LEASE, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise cli.UsageError(f"another fm night is already running for {p.slug}")
    if not getattr(args, "dry_run", False):
        try:  # T-0452: standing orders capture their tasks (no session runs, so no spend)
            from sampo import orders
            orders.run(p)
        except Exception as e:  # an order file never costs the night
            print(f"sampo: warning: standing orders: {e}", file=sys.stderr)
    bad = [n for n in args.only or [] if n not in NAMES]
    if bad:
        raise cli.UsageError(f"no such night job: {', '.join(bad)} (jobs: {', '.join(NAMES)})")
    high, known = _usage(p)
    if high:  # T-2593: a deliberate skip is a success to systemd, not a failed run doctor and foresight flag
        return cli.out(args, {"skipped": f"usage is high ({high})", "ran": []},
                         f"usage is high ({high}): nothing runs tonight — the user's own work comes first")
    if not args.max_usd >= 0:  # NaN too
        raise cli.UsageError("--max-usd must be a number ≥ 0")
    cur, _ = budget.effective(p)
    start, _ = budget.spent()
    limit = max(0.0, min(args.max_usd, cur["day"] - start)) / (1 if known else 2)
    limit = round(limit, 4)
    note = "" if known else f"usage unknown (no statusline snapshot from the last {STALE_H} h): the limit is halved"
    if c.read_meta(p).get("sensitive"):  # review: nothing of a sensitive project leaves for children or replays
        return cli.out(args, {"jobs": [], "skipped": [], "ran": [], "limit": limit,
                                "note": "sensitive project: nothing runs at night"},
                         "night: sensitive project: nothing runs at night")
    plan, skipped, total = [], [], 0.0
    for name, argv, est in jobs(p):
        if args.only and not any(name == n or name.startswith(n + " ") for n in args.only):
            continue
        (plan if total + est <= limit else skipped).append({"name": name, "argv": argv, "usd": est})
        total += est if total + est <= limit else 0
    head = f"night: {len(plan)} job(s), about ${total:.2f} of ${limit:.2f}" + (f" ({note})" if note else "")
    rows = [f"  {j['name']}: fm {' '.join(j['argv'])} (≈ ${j['usd']:.2f})" for j in plan] + \
           [f"  skipped {j['name']} (≈ ${j['usd']:.2f}: over the limit)" for j in skipped]
    if args.dry_run:
        return cli.out(args, {"jobs": plan, "skipped": skipped, "limit": limit, "note": note}, "\n".join([head] + rows))
    ran, stopped, t0 = [], "", c.now()
    from sampo.moonscale import BUSY_EXIT as BUSY  # T-3929: a moons store another run holds is busy, not a failure
    for j in plan:  # one at a time; before each, the real spend so far and the usage now (review)
        night_usd = budget.spent()[0] - start
        if night_usd > limit and ran:  # the spend stop: real spend past the limit ends the night
            stopped = f"spend ${night_usd:.2f} reached the limit ${limit:.2f}"
            break
        if j["usd"] > 0 and night_usd >= limit and ran:  # T-3929: at the limit a paid job waits; a free one still runs
            skipped.append(j)
            continue
        if budget.usage_high(p):
            stopped = f"usage climbed ({budget.usage_high(p)})"
            break
        if c.panicked():
            stopped = c.PAUSED
            break
        real = [x["exit"] for x in ran if x["exit"] != BUSY]  # T-3929: a busy exit neither trips nor resets the breaker
        if len(real) >= serve.FAILS_MAX and all(real[-serve.FAILS_MAX:]):  # T-0434: the breaker
            stopped = f"{serve.FAILS_MAX} jobs failed in a row"
            break
        if j["name"] == "kalevala rounds":  # T-3082: the rounds run here until the night's limit or a halt stops them
            ran.append(_kalevala_rounds(p, start, limit))
            continue
        serve.beat(p, "night", JOB_TIMEOUT, j["name"])
        try:
            left = ["--max-usd", f"{max(0.0, limit - (budget.spent()[0] - start)):.4f}"] if j["name"] == "queue" else []
            r = subprocess.run([sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "fm"), *j["argv"], *left], cwd=p.root,
                               env=dict(os.environ, FOREMAN_NO_BACKGROUND="1"), capture_output=True, text=True,
                               timeout=JOB_TIMEOUT, preexec_fn=priority(j["name"]))
            code, tail = r.returncode, (r.stdout + r.stderr).strip().splitlines()[-1:]
        except (OSError, subprocess.TimeoutExpired) as e:
            code, tail = 124, [str(e)]
        if code == BUSY:  # T-3929: logged as busy; the next job still runs and the breaker never counts it
            tail = ["busy: " + " ".join(tail)]
        ran.append({"name": j["name"], "exit": code, "busy": code == BUSY, "tail": c.fit(c.plain(" ".join(tail)), 160)})
    if not stopped and ran and budget.spent()[0] - start > limit:
        stopped = f"spend ${budget.spent()[0] - start:.2f} passed the limit ${limit:.2f} in the last job"
    closed = [e["task"] for e in c.ledger_tail(p, 5000, kinds=("task_done",)) if e.get("event") == "task_done" and e.get("task")
              and str(e.get("ts", ""))[:19] >= t0[:19]] if any(x["name"] == "queue" for x in ran) else []
    with c.lock(p.dir):
        c.log_event(p, "night", data={"ran": ran, "skipped": [s["name"] for s in skipped], "limit": limit,
                                      "stopped": stopped, "closed": closed}, session=cli.session())
    cli.out(args, {"ran": ran, "skipped": skipped, "limit": limit, "stopped": stopped, "note": note},
              "\n".join([head] + [f"  {'·' if x.get('busy') else '✓' if x['exit'] == 0 else '✗'} {x['name']}: {x['tail']}" for x in ran]
                        + [f"  skipped {s['name']}" for s in skipped] + ([f"  stopped: {stopped}"] if stopped else [])))
