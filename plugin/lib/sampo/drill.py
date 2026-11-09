"""T-1669: fm drill. A scripted day (fm verbs and the hooks Claude Code calls) runs in a temp home with faults planted
mid-day: a torn ledger and journal, a stale lock, a broken lane, a corrupt state file, an unreadable file. It passes only
when every fault is found (fm fsck, fm verify-ledger) or healed, nothing printed a traceback, and once the faults are
repaired the day's work is still there. A seed replays the same plan."""
import json
import os
import random
import re
import shutil
import stat
import subprocess
import tempfile
import time

from sampo import core as c

KINDS = ("torn ledger", "torn journal", "stale lock", "broken lane", "corrupt state", "unreadable",
         "dropped class")
STATE_FILES = ("meta.json", "gate.json", "status.json", "skill-index.json")  # the project files fm reads whole
TB = "Traceback (most recent call last)"
CUT = '{"drill": "cut'  # a planted torn line: no closing quote, brace or newline
DAY = (  # (label, fm args) or (label, hook event, payload): the work faults land in the middle of
    ("fm status", ("status",)),
    ("UserPromptSubmit", "UserPromptSubmit", {"prompt": "fix the thing"}),
    ("PreToolUse Edit", "PreToolUse", {"tool_name": "Edit", "tool_input": {
        "file_path": "README.md", "old_string": "x", "new_string": "y"}}),
    ("fm task log", ("task", "log", "T-0001", "steer: drill")),
    ("fm task evidence", ("task", "evidence", "T-0001", "--step", "1", "--run", "true")),
    ("fm checkpoint", ("checkpoint",)),
    ("Stop", "Stop", {"stop_hook_active": False}),
    ("fm status", ("status",)),
)


def plan(seed, faults=3):
    """The faults a seed plants: [{kind, at (the day step it lands before)[, target]}], the same for the same seed."""
    if not 3 <= faults <= len(KINDS):
        raise ValueError(f"a drill plants 3 to {len(KINDS)} faults, not {faults}")
    rng = random.Random(seed)
    out = []
    for kind in rng.sample(KINDS, faults):
        f = {"kind": kind, "at": rng.randrange(len(DAY))}
        if kind == "corrupt state":
            f["target"] = rng.choice(STATE_FILES)
        out.append(f)
    return out


def crash(label, rc, out, err):
    """A step that printed a traceback, as one line naming it; None when it didn't (a clean refusal isn't a crash)."""
    text = (out or "") + (err or "")
    if TB not in text:
        return None
    last = [ln for ln in text.strip().splitlines() if ln.strip()][-1:]
    return f"{label} (exit {rc}): {last[0].strip()[:200] if last else 'traceback'}"


class Day:
    """One temp home, its repo and the commands that run there; nothing outside it is touched."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="fm-drill-")
        h = self.home
        self.env = c.sandbox_env(os.environ, drop=("FOREMAN_", "GIT_"), FOREMAN_HOME=f"{h}/home",
                                 XDG_STATE_HOME=f"{h}/xdg", HOME=f"{h}/h", GIT_CONFIG_GLOBAL=f"{h}/gitconfig",
                                 FOREMAN_NO_BACKGROUND="1", FOREMAN_NO_LINK="1", FOREMAN_INHIBITED="1",
                                 FOREMAN_DOCTOR_BENCH="0", GIT_AUTHOR_NAME="drill", GIT_AUTHOR_EMAIL="drill@localhost",
                                 GIT_COMMITTER_NAME="drill", GIT_COMMITTER_EMAIL="drill@localhost")  # T-2822
        for d in ("home", "h", "repo"):
            os.makedirs(os.path.join(h, d))
        self.repo, self.state = f"{h}/repo", f"{h}/home/state"
        self.crashes, self.log = [], os.path.join(self.state, "logs", "hooks.log")
        self.git("init", "-q", "-b", "main")
        with open(os.path.join(self.repo, "README.md"), "w") as f:
            f.write("x\n")
        self.git("add", ".")
        self.git("commit", "-qm", "init")

    def git(self, *a):
        subprocess.run(["git", "-C", self.repo, *a], env=self.env, check=True, capture_output=True, timeout=60)

    def _run(self, label, argv, stdin=None):
        try:
            p = subprocess.run(argv, cwd=self.repo, env=self.env, input=stdin, capture_output=True, text=True,
                               timeout=120)
        except subprocess.TimeoutExpired:
            self.crashes.append(f"{label}: timed out after 120 s")
            return subprocess.CompletedProcess(argv, -1, "", "")
        why = crash(label, p.returncode, p.stdout, p.stderr)
        if why:
            self.crashes.append(why)
        return p

    def fm(self, *a):
        return self._run("fm " + " ".join(a[:2]), [os.path.join(c.PLUGIN_ROOT, "bin", "fm"), *a])  # not a list: a path or word fragment, not a list

    def hook(self, event, payload):
        if "tool_input" in payload:
            ti = dict(payload["tool_input"], file_path=os.path.join(self.repo, payload["tool_input"]["file_path"]))
            payload = dict(payload, tool_input=ti)
        return self._run(event, [os.path.join(c.PLUGIN_ROOT, "hooks", "hook"), event],
                         json.dumps({"session_id": "drill-1", "cwd": self.repo, **payload}))

    def step(self, s):
        return self.fm(*s[1]) if len(s) == 2 else self.hook(s[1], s[2])

    def pdir(self):
        return os.path.join(self.state, "projects", os.listdir(os.path.join(self.state, "projects"))[0])

    def remove(self):
        for d, dirs, names in os.walk(self.home):  # an unreadable fault left behind would stop rmtree
            for n in dirs + names:
                try:
                    os.chmod(os.path.join(d, n), 0o700, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
        shutil.rmtree(self.home, ignore_errors=True)


def _target(day, f, kinds):
    k = f["kind"]
    if k in ("torn ledger", "dropped class"):
        return os.path.join(day.pdir(), "ledger.jsonl")
    if k == "torn journal":
        return os.path.join(day.state, "events.jsonl")
    if k == "stale lock":
        return os.path.join(day.state, "ambient", "run.pid")
    if k == "broken lane":
        return os.path.join(day.home, "gone-lane")
    if k == "corrupt state":
        return os.path.join(day.pdir(), f["target"])
    # unreadable: a journal the drill's torn journal isn't using, else the state's own lib-names.json
    return os.path.join(day.state, "lib-names.json" if "torn journal" in kinds else "events.jsonl")


def _plant(day, f, saved):
    k, path = f["kind"], f["path"]
    if k in ("torn ledger", "torn journal"):
        c.append_line(path, "")  # whole lines before the cut, so the cut line is the planted one alone
        with open(path, "a") as fh:
            fh.write(CUT)
    elif k == "dropped class":  # a done task whose every task_done line is gone: the briefs say closed, the ledger doesn't
        brief = _brief_path(day, "T-0003")
        with open(brief) as fh:
            text = fh.read()
        c.write_atomic(brief, re.sub(r"(?m)^status: .*$", "status: done", text, count=1))
        with open(path, encoding="utf-8", errors="replace") as fh:
            keep = [ln for ln in fh.read().splitlines(True) if '"task_done"' not in ln]
        with open(path, "w") as fh:
            fh.writelines(keep)
    elif k == "stale lock":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("999999999\n")  # above any pid_max: no process holds it
        old = time.time() - 7200
        os.utime(path, (old, old))
    elif k == "broken lane":
        brief = _brief_path(day, "T-0002")
        with open(brief) as fh:
            text = fh.read()
        # a hand edit: the lane a crash left named after its folder went
        c.write_atomic(brief, text.replace("\nallow:", f"\nlane: {path}\nlane_branch: foreman/drill-gone\nallow:", 1))
    elif k == "corrupt state":
        try:
            with open(path) as fh:
                saved[path] = fh.read()
        except FileNotFoundError:  # not written yet today: the repair removes it
            saved[path] = None
        with open(path, "w") as fh:
            fh.write('{"cut')
    elif k == "unreadable":
        if not os.path.exists(path):
            c.append_line(path, "{}\n")
        saved[path] = stat.S_IMODE(os.stat(path).st_mode)
        os.chmod(path, 0)


def _brief_path(day, tid):
    tasks = os.path.join(day.pdir(), "tasks")
    return os.path.join(tasks, next(n for n in sorted(os.listdir(tasks)) if n.startswith(tid + "-")))


def _verdict(day, f, found, ledger):
    """(verdict, detail): found by fsck or verify-ledger, healed by the day, skipped, or missed."""
    k, path = f["kind"], f["path"]
    if k == "dropped class":  # not fsck's to find: the readers that lean on task_done must say it's gone
        bare = [r for r in ("outcomes", "digest") if "no task_done events" not in day.fm(r).stdout]
        if bare:
            return "missed", f"fm {' and fm '.join(bare)} printed a rate as if task_done events were there"
        return "found", "fm outcomes and fm digest both name the missing task_done class"
    mine = [x for x in found if x["kind"] == k and (x["path"] == path or k == "broken lane" and x.get("task") == "T-0002")]
    if k == "torn ledger" and not mine:
        mine = [x for x in found if x["kind"] == "torn journal" and x["path"] == path]
    if mine:
        return "found", f"fm fsck: {mine[0]['detail']}"
    if k == "unreadable" and f.get("skipped"):
        return "skipped", "running as root, which reads any file"
    if k == "torn ledger" and (ledger.get("broken") or ledger.get("gaps") or ledger.get("damaged")):
        return "found", "fm verify-ledger: " + str(ledger.get("broken") or "unchained or damaged lines")
    try:
        if k in ("torn ledger", "torn journal"):
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            i = lines.index(CUT) if CUT in lines else -1
            if i >= 0 and i + 1 < len(lines):
                for ln in lines[i + 1:]:
                    json.loads(ln)
                return "healed", "the cut line stands alone and every later line parses"
        elif k == "stale lock" and not os.path.exists(path):
            return "healed", "the lock was cleared"
        elif k == "corrupt state":
            with open(path) as fh:
                json.load(fh)
            return "healed", f"{f['target']} was rewritten whole"
    except (OSError, ValueError):
        pass
    return "missed", "neither fm fsck nor fm verify-ledger named it, and the day didn't heal it"


def drill(seed, faults=3):
    """Run one drill; the report: {ok, seed, faults, crashes, home, replay, checks}."""
    planned = plan(seed, faults)
    kinds = {f["kind"] for f in planned}
    day, saved, checks = Day(), {}, []
    try:
        day.fm("init")
        day.fm("task", "new", "Drill a thing", "--type", "FIX", "--tier", "S", "--ac", "works :: true", "--step",
               "do it", "--focus")
        day.fm("capture", "a later idea")
        day.fm("capture", "a closed idea")  # T-0003: the dropped-class fault marks it done (T-0002 is the broken lane's)
        for f in planned:
            f["path"] = _target(day, f, kinds)
            f["skipped"] = f["kind"] == "unreadable" and os.geteuid() == 0
        for i, s in enumerate(DAY):
            for f in planned:
                if f["at"] == i and not f["skipped"]:
                    _plant(day, f, saved)
            day.step(s)
        fs = day.fm("fsck", "--json")
        found = json.loads(fs.stdout or "{}").get("findings", [])
        led = day.fm("verify-ledger", "--json")
        try:
            ledger = json.loads(led.stdout or "{}")
        except ValueError:
            ledger = {}
        for f in planned:
            f["verdict"], f["detail"] = _verdict(day, f, found, ledger)
        for path, v in saved.items():  # repair as a person would: permissions back, the corrupt file restored
            if isinstance(v, int):
                os.chmod(path, v)
            elif v is None:
                os.unlink(path)
            else:
                with open(path, "w") as fh:
                    fh.write(v)
        if any(f["verdict"] == "missed" for f in planned):  # a corrupt meta.json hides its project's lanes: fsck
            again = json.loads(day.fm("fsck", "--json").stdout or "{}").get("findings", [])  # again, as a person would
            for f in planned:
                if f["verdict"] == "missed":
                    v, d = _verdict(day, f, again, {})
                    if v == "found":
                        f["verdict"], f["detail"] = v, f"once the rest was repaired, {d}"
        show = day.fm("task", "show", "T-0001")
        checks.append({"check": "the day's task survives the repair", "ok": show.returncode == 0,
                       "detail": (show.stderr or "").strip()[:200]})
        if os.path.exists(day.log):
            with open(day.log, encoding="utf-8", errors="replace") as fh:
                n = fh.read().count(TB)
            if n:
                day.crashes.append(f"hooks.log: {n} traceback{'s' if n != 1 else ''} ({day.log})")
    except Exception as e:  # the drill's own failure is reported, the home kept to look at
        day.crashes.append(f"drill: {type(e).__name__}: {e}")
    ok = not day.crashes and all(x["ok"] for x in checks) and all(
        f.get("verdict") in ("found", "healed", "skipped") for f in planned)
    rep = {"ok": ok, "seed": seed, "faults": [{k: v for k, v in f.items() if k != "skipped"} for f in planned],
           "crashes": day.crashes, "checks": checks, "home": day.home,
           "replay": f"fm drill --seed {seed} --faults {faults}"}
    if ok:
        day.remove()
    return rep


RESTORE_CHECKS = (("ledger chain", "verify-ledger"), ("fsck", "fsck"), ("readers", "state"), ("readers", "outcomes"))


def restore(name=None):
    """T-2652: a host-wipe rehearsal. An upgrade snapshot (fm rollback's, checked against its manifest) is laid into a
    temp home, and each project's record must read back: its ledger chain verifies, fsck is clean, and fm state and fm
    outcomes run without a traceback. Only read-only verbs run, so a restored meta naming a real repo root writes
    nothing there; the live state is never touched."""
    import tarfile

    from sampo import rollback
    names = rollback._listing()
    name = name or next((s for s in names if not s.startswith(rollback.PRE)), None)
    if name not in names:
        raise ValueError(f"no snapshot named {name}; fm rollback --list shows them" if name else
                         "no snapshot yet: one is taken when Sampo's version changes (fm rollback --list)")
    day, rows = Day(), []
    stage = os.path.join(day.home, "stage")
    try:
        slugs = rollback._stage(os.path.join(rollback._dir(), name), stage, True)
    except (ValueError, OSError, tarfile.TarError) as e:
        rows.append({"project": "(snapshot)", "ok": False, "detail": f"won't unpack: {e}"})
        slugs = {}
    for slug in sorted(slugs):
        shutil.copytree(os.path.join(stage, slug), os.path.join(day.state, "projects", slug))
        bad = []
        for label, verb in RESTORE_CHECKS:
            n = len(day.crashes)
            r = day.fm(verb, "-p", slug)
            if r.returncode or len(day.crashes) > n:
                last = [ln for ln in ((r.stdout or "") + (r.stderr or "")).splitlines() if ln.strip()][-1:]
                bad.append(f"{label} (fm {verb}, exit {r.returncode}): {last[0].strip()[:160] if last else ''}")
        rows.append({"project": slug, "ok": not bad, "detail": "; ".join(dict.fromkeys(bad))})
    ok = bool(rows) and all(x["ok"] for x in rows)
    rep = {"ok": ok, "snapshot": name, "projects": rows, "home": day.home}
    if ok:
        day.remove()
    return rep


def render_restore(r):
    lines = [f"fm drill --restore: {r['snapshot']}, {len(r['projects'])} project(s) laid into a temp home"]
    lines += [f"  ✓ {x['project']}: ledger chain, fsck, readers" if x["ok"] else f"  ✗ {x['project']}: {x['detail']}"
              for x in r["projects"]]
    lines.append("OK: the snapshot restores and reads back" if r["ok"] else
                 f"FAILED: the temp home is kept for a look: {r['home']}")
    return "\n".join(lines)


def render(r):
    lines = [f"fm drill: seed {r['seed']}, {len(r['faults'])} faults ({r['replay']} replays it)"]
    for f in r["faults"]:
        mark = "✓" if f.get("verdict") in ("found", "healed", "skipped") else "✗"
        lines.append(f"  {mark} {f['kind']:<13} {f.get('verdict', '?'):<7} before step {f['at'] + 1} "
                     f"({DAY[f['at']][0]}): {c.plain(f.get('detail', ''))}")
    for x in r["checks"]:
        lines.append(f"  {'✓' if x['ok'] else '✗'} {x['check']}" + (f": {x['detail']}" if not x["ok"] else ""))
    lines += [f"  ✗ crash: {c.plain(x)}" for x in r["crashes"]]
    lines.append("OK" if r["ok"] else f"FAILED: the temp home is kept for a look: {r['home']}")
    return "\n".join(lines)


def cmd_drill(args):
    from sampo import cli
    if getattr(args, "restore", None) is not None:  # T-2652
        try:
            r = restore(args.restore or None)
        except ValueError as e:
            raise cli.UsageError(str(e)) from None
        cli.out(args, r, render_restore(r))
        return 0 if r["ok"] else 1
    seed = args.seed if args.seed is not None else int(time.time()) % 1_000_000
    try:
        r = drill(seed, args.faults)
    except ValueError as e:
        raise cli.UsageError(str(e)) from None
    cli.out(args, r, render(r))
    return 0 if r["ok"] else 1
