"""T-3747: sampo bench tbench runs Terminal-Bench tasks through Sampo's own agent loop (Smith's actions and prompt, any
model profile) in a container per task: the task set fetched as data (a shallow git clone, no harness installed), the
image built from the task's own Dockerfile, the agent acting through docker exec inside the task's time limit, the
task's own tests grading it. Results and one trajectory per task go in <state>/bench/tbench/<run>/; each container and
image is removed after its task, and a build never starts under SAMPO_BENCH_MIN_FREE_GB (default 40) free.

Two task layouts load: the original (task.yaml, Dockerfile, run-tests.sh, tests/, pass = run-tests.sh exits 0) and
Terminal-Bench 2.0 (task.toml, instruction.md, environment/Dockerfile, tests/test.sh, pass = /logs/verifier/reward.txt
reads 1)."""
import glob
import json
import os
import re
import shlex
import subprocess
import time
import tomllib

from sampo import core, dockershim, models as m

REPOS = ("https://github.com/laude-institute/terminal-bench-2.git", "https://github.com/laude-institute/terminal-bench.git")
MIN_FREE_GB = 40
RUN_SEC = 120  # one command in the container
READ_LINES = 200


class Stop(Exception):
    """The run can't go on (no docker, the disk budget): the reason is the message."""


def cache_dir():
    return os.path.join(os.path.expanduser("~"), ".cache", "claude-ops", "bench")


def fetch_tasks(root=None):
    """The task set as data: a shallow clone into the cache (once), the folder holding the tasks."""
    root = root or cache_dir()
    for url in REPOS:
        dest = os.path.join(root, os.path.basename(url)[:-4])
        if not os.path.isdir(dest):
            os.makedirs(root, exist_ok=True)
            try:
                p = subprocess.run(["git", "clone", "--depth", "1", "-q", url, dest], capture_output=True, text=True,
                                   timeout=600)
            except (subprocess.TimeoutExpired, OSError) as e:
                raise Stop(f"fetching {url} failed: {e}")
            if p.returncode:
                continue
        tasks = os.path.join(dest, "tasks")
        d = tasks if os.path.isdir(tasks) else dest
        if load_tasks(d):
            return d
    raise Stop(f"no Terminal-Bench task set fetched into {root}: clone one of {', '.join(REPOS)} there, or pass --tasks DIR")


def _yaml(text):
    """The top-level scalars and block scalars of a task.yaml (all it needs; no YAML library is installed)."""
    out, lines, i = {}, text.splitlines(), 0
    while i < len(lines):
        mt = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", lines[i])
        i += 1
        if not mt:
            continue
        k, v = mt.groups()
        if v[:1] in ("|", ">"):
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in " \t"):
                block.append(lines[i])
                i += 1
            ind = min((len(x) - len(x.lstrip()) for x in block if x.strip()), default=0)
            body = [x[ind:] for x in block]
            v = (" " if v[0] == ">" else "\n").join(body).strip("\n") if v[1:2] != "+" else "\n".join(body)
        out[k] = v.strip().strip("\"'") if v[:1] not in ("|", ">") else v
    return out


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _workdir(dockerfile):
    try:
        with open(dockerfile, encoding="utf-8", errors="replace") as f:
            found = re.findall(r"^\s*WORKDIR\s+(\S+)", f.read(), re.M)
    except OSError:
        return "/app"
    return found[-1] if found and found[-1].startswith("/") else "/app"


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def load_task(d):
    """One task folder as a dict, or None when it isn't a task."""
    tid = os.path.basename(d)
    try:
        if os.path.isfile(os.path.join(d, "task.toml")):
            with open(os.path.join(d, "task.toml"), "rb") as f:
                t = tomllib.load(f)
            ctx = os.path.join(d, "environment")
            return {"id": tid, "dir": d, "layout": "v2", "category": str((t.get("metadata") or {}).get("category") or ""),
                    "instruction": _read(os.path.join(d, "instruction.md")).strip(), "context": ctx,
                    "agent_sec": _num((t.get("agent") or {}).get("timeout_sec"), 900),
                    "test_sec": _num((t.get("verifier") or {}).get("timeout_sec"), 900),
                    "workdir": _workdir(os.path.join(ctx, "Dockerfile"))}
        if os.path.isfile(os.path.join(d, "task.yaml")):
            y = _yaml(_read(os.path.join(d, "task.yaml")))
            return {"id": tid, "dir": d, "layout": "v1", "category": y.get("category", ""),
                    "instruction": (y.get("instruction") or "").strip(), "context": d,
                    "agent_sec": _num(y.get("max_agent_timeout_sec"), 360),
                    "test_sec": _num(y.get("max_test_timeout_sec"), 60),
                    "workdir": _workdir(os.path.join(d, "Dockerfile"))}
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        pass
    return None


def load_tasks(root):
    return [t for t in (load_task(os.path.join(root, n)) for n in sorted(os.listdir(root)) if os.path.isdir(os.path.join(root, n)))
            if t and t["instruction"] and os.path.isfile(os.path.join(t["context"], "Dockerfile"))]


def pick(tasks, category=None, n=20, only=None):
    """The subset: --only ids, else the category's tasks, else every category in turn (so 20 isn't one corner); n=None: all."""
    if only:
        tasks = [t for t in tasks if t["id"] in only]
    elif category:
        tasks = [t for t in tasks if t["category"] == category]
    else:
        by = {}
        for t in tasks:
            by.setdefault(t["category"], []).append(t)
        tasks = [by[k][i] for i in range(max(map(len, by.values()), default=0)) for k in sorted(by) if i < len(by[k])]
    return tasks if n is None else tasks[:n]


# ---------------------------------------------------------------- the agent

PROMPT = (f"You are working in a Linux container; the task's own tests will grade what you leave there. Use the "
          f"actions to do the task, one at a time, then call done.")


def _num_lines(text, start, lines):
    rows = text.splitlines()
    a = max(1, start) - 1
    return "\n".join(f"{i + 1}\t{x}" for i, x in enumerate(rows[a:a + lines], a)) or "(empty)"


def _cat(box, path):
    text = box.fetch(path)
    if text is None:
        raise OSError(f"{path}: no such file")
    return text


def _put(box, path, text):
    ok, out = box.store(path, text)
    if not ok:
        raise OSError(f"{path}: {out.strip()[-200:]}")


def act(box, name, a, left):
    """(result text, finished) of one action in the container; a bad argument is an error text the model sees."""
    try:
        if name == "read":
            p = str(a["path"])
            code, out = box.exec(f"test -d {shlex.quote(p)} && ls -la -- {shlex.quote(p)}", seconds=30)
            if code == 0:
                return out, False
            return _num_lines(_cat(box, p if p.startswith("/") else f"{box.workdir}/{p}"), int(a.get("start") or 1),
                              min(int(a.get("lines") or READ_LINES), READ_LINES)), False
        if name == "search":
            g = f" --include={shlex.quote(str(a['glob']))}" if a.get("glob") else ""
            return box.exec(f"grep -rnE{g} -- {shlex.quote(str(a['pattern']))} . | head -100", seconds=30)[1] or "no match", False
        if name in ("edit", "write"):
            p = str(a["file"])
            full = p if p.startswith("/") else f"{box.workdir}/{p}"
            if name == "write":
                _put(box, full, str(a["content"]))
                return f"wrote {p} ({len(str(a['content']))} characters)", False
            text, old = _cat(box, full), str(a["old"])
            if text.count(old) != 1:
                return f"error: old must occur exactly once in {p}; it occurs {text.count(old)} times", False
            _put(box, full, text.replace(old, str(a["new"])))
            return "edited", False
        if name == "run":
            code, out = box.exec(str(a["cmd"]), seconds=max(1, min(RUN_SEC, left)))
            return (f"timed out" if code is None else f"exit {code}") + (f"\n{out}" if out else ""), False
        if name == "done":
            return "done", True
        return f"error: no action {name!r}; the actions are read, search, edit, write, run, done", False
    except (KeyError, ValueError, TypeError) as e:
        return f"error: {name} needs its arguments ({type(e).__name__}: {e})", False
    except OSError as e:
        return f"error: {e}", False


BARE_TOOL = [{"name": "run", "description": "Run a shell command in the container; returns its exit code and output.",
              "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}]
HARNESSES = ("bare", "sampo")


def bare_agent(profile, task, box, log, max_steps=40):
    """The control (T-3747): the model alone in a fixed minimal loop: the task text, one tool (a shell command), the
    task's time and the same token budget as Sampo's; no Sampo prompt, tools, trimming or nudges. It ends when the
    model answers without a command."""
    from sampo import smith
    msgs = [{"role": "user", "content": task["instruction"]}]
    tools = BARE_TOOL if profile.get("tools") else None
    end = time.time() + task["agent_sec"]
    for step in range(max_steps):
        left = end - time.time()
        if left <= 0:
            return "agent_timeout"
        try:
            r = m.complete(profile, msgs, tools=tools, max_tokens=smith.MAX_TOKENS, timeout=max(5, min(600, left)))
        except (m.ModelError, OSError, ValueError) as e:
            log("model_error", error=str(e)[:300])
            return "model_error"
        log("model", text=(r.get("text") or "")[:1500], usage=r.get("usage"))
        calls = r.get("tool_calls") or []
        if not calls and not tools:  # no native tools: a JSON {"cmd": ...} in the text is the one command
            v = m.extract_json(r.get("text") or "")
            calls = [{"id": f"t{step}", "name": "run", "args": v}] if isinstance(v, dict) and "cmd" in v else []
        if not calls:
            return "done"
        as_text = not r.get("tool_calls")
        msgs.append({"role": "assistant", "content": r.get("text") or "", **({} if as_text else {"tool_calls": calls})})
        for tc in calls:
            cmd = (tc.get("args") or {}).get("cmd") if isinstance(tc.get("args"), dict) else None
            code, out = box.exec(str(cmd), seconds=max(1, min(RUN_SEC, end - time.time()))) if cmd else (None, "no cmd")
            res = ("timed out" if code is None else f"exit {code}") + (f"\n{out}" if out else "")
            log("action", action="run", args=json.dumps({"cmd": cmd})[:600], result=res[:1500])
            msgs.append({"role": "user", "content": f"Result of run:\n{res}"} if as_text else
                         {"role": "tool", "tool_call_id": tc["id"], "content": res})
    return "max_steps"


def agent(profile, task, box, log, max_steps=40):
    """Smith's actions against the container until done, the task's time or the step limit. Returns how it ended."""
    from sampo import smith
    tools = smith.TOOLS if profile.get("tools") else None
    system = smith.ACT_PROMPT + ("" if tools else smith.TEXT_ACTIONS) + "\n" + PROMPT + \
        f"\nPaths are relative to {box.workdir}."
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": task["instruction"]}]
    end = time.time() + task["agent_sec"]
    for step in range(max_steps):
        left = end - time.time()
        if left <= 0:
            return "agent_timeout"
        try:
            r = m.complete(profile, smith.trim(msgs), tools=tools, max_tokens=smith.MAX_TOKENS, timeout=max(5, min(600, left)))
        except (m.ModelError, OSError, ValueError) as e:
            log("model_error", error=str(e)[:300])
            return "model_error"
        log("model", text=(r.get("text") or "")[:1500], usage=r.get("usage"))
        calls = r.get("tool_calls") or []
        if not calls:
            v = m.extract_json(r.get("text") or "")
            if isinstance(v, dict) and isinstance(v.get("action"), str):
                calls = [{"id": f"t{step}", "name": v.pop("action"), "args": v}]
            else:
                msgs += [{"role": "assistant", "content": r.get("text") or ""},
                         {"role": "user", "content": "No action found." + smith.TEXT_ACTIONS}]
                continue
        as_text = not r.get("tool_calls")
        msgs.append({"role": "assistant", "content": r.get("text") or "", **({} if as_text else {"tool_calls": calls})})
        finished = False
        for tc in calls:
            a = smith.canon_args(tc["name"], tc["args"])
            res, fin = act(box, tc["name"], a, end - time.time())
            log("action", action=tc["name"], args=json.dumps(a)[:600], result=res[:1500])
            finished = finished or fin
            msgs.append({"role": "user", "content": f"Result of {tc['name']}:\n{res}"} if as_text else
                        {"role": "tool", "tool_call_id": tc["id"], "content": res})
        if finished:
            return "done"
    return "max_steps"


# ---------------------------------------------------------------- grading and the run

def grade(task, box):
    """True when the task's own tests pass in the container the agent left."""
    t = max(5, int(task["test_sec"]))
    if task["layout"] == "v2":
        box.copy_in(os.path.join(task["dir"], "tests"), "/tests")
        box.exec("mkdir -p /logs/verifier", seconds=30)
        code, out = box.exec("bash /tests/test.sh", seconds=t)
        try:
            return float(_cat(box, "/logs/verifier/reward.txt").strip()) >= 1, out
        except (OSError, ValueError):
            return code == 0, out
    box.copy_in(os.path.join(task["dir"], "tests"), "/tests")
    box.copy_in(os.path.join(task["dir"], "run-tests.sh"), "/tests/run-tests.sh")
    code, out = box.exec("TEST_DIR=/tests bash /tests/run-tests.sh", seconds=t)
    return code == 0, out


def _check_room():
    need = float(os.environ.get("SAMPO_BENCH_MIN_FREE_GB", MIN_FREE_GB))
    have = dockershim.free_gb()
    if have < need:
        raise Stop(f"only {have:.0f} GB free, under the {need:.0f} GB budget: free disk (or set SAMPO_BENCH_MIN_FREE_GB)")


def run_task(profile, task, out, max_steps=40, harness="sampo"):
    """One task end to end; the container and the image are gone after it, whatever happened."""
    stamp = f"{os.getpid()}-{int(time.time())}-{harness}"
    tag = f"sampo-bench-tb-{re.sub(r'[^a-z0-9_.-]', '-', task['id'].lower())}:{stamp}"
    name = f"sampo-bench-tb-{re.sub(r'[^a-z0-9_.-]', '-', task['id'].lower())}-{stamp}"
    rec = {"id": task["id"], "harness": harness, "category": task["category"], "passed": False, "ended": None,
           "seconds": 0}
    t0 = time.time()
    with open(os.path.join(out, f"{task['id']}.{harness}.jsonl"), "w", encoding="utf-8") as tr:
        def log(event, **d):
            tr.write(json.dumps(dict(d, event=event, ts=round(time.time() - t0, 1)), default=str) + "\n")
            tr.flush()
        log("start", task=task["id"], harness=harness, instruction=task["instruction"][:2000], agent_sec=task["agent_sec"])
        try:
            _check_room()
            dockershim.build(tag, task["context"])
            with dockershim.Container(name, tag, task["workdir"], prefix="") as box:  # removed on the way out
                rec["ended"] = (bare_agent if harness == "bare" else agent)(profile, task, box, log, max_steps)
                rec["passed"], tests = grade(task, box)
                log("graded", passed=rec["passed"], tests=core.redact(tests[-1500:]))
        except dockershim.DockerError as e:  # a build or start that fails is the task's result, not the run's
            rec["ended"] = rec["ended"] or "build_failed"
            log("error", error=str(e)[:600])
        except OSError as e:
            rec["ended"] = rec["ended"] or "grade_failed"
            log("error", error=str(e)[:600])
        finally:
            dockershim.rmi(tag)
    rec["seconds"] = round(time.time() - t0, 1)
    return rec


def summary(res):
    """{harness: passed count} over the finished tasks."""
    return {h: sum(x["passed"] for x in res["tasks"] if x["harness"] == h) for h in res["harnesses"]}


def run_bench(model, tasks_dir, out, n=20, category=None, only=None, max_steps=40, harnesses=HARNESSES):
    """Each chosen task runs once per harness (bare = the model alone, sampo = with Sampo's agent), same profile."""
    if why := dockershim.unavailable():
        raise Stop(f"{why}: sampo bench tbench builds each task's image with docker")
    _check_room()
    profile = m.resolve(model)
    chosen = pick(load_tasks(tasks_dir), category, n, only)
    if not chosen:
        raise Stop(f"no task matches in {tasks_dir}" + (f" for category {category!r}" if category else ""))
    os.makedirs(out, exist_ok=True)
    res = {"model": profile.get("name"), "harnesses": list(harnesses), "tasks": [], "n": len(chosen), "passed": {},
           "stopped": None}
    try:
        for t in chosen:
            for h in harnesses:
                res["tasks"].append(run_task(profile, t, out, max_steps, h))
                res["passed"] = summary(res)
                core.write_atomic(os.path.join(out, "results.json"), json.dumps(res, indent=1))
    except Stop as e:  # the disk budget hit between tasks: keep what finished
        res["stopped"] = str(e)
    res["passed"] = summary(res)
    core.write_atomic(os.path.join(out, "results.json"), json.dumps(res, indent=1))
    return res


def table(r):
    """Side by side: passes per harness out of n, and sampo minus bare."""
    p, n = r["passed"], r["n"]
    cells = "  ".join(f"{h} {p.get(h, 0)}/{n}" for h in r["harnesses"])
    return cells + (f"  delta {p['sampo'] - p['bare']:+d}" if "bare" in p and "sampo" in p else "")


def saved_runs():
    """[(row, text)] for every saved tbench run (sampo bench runs lists them after bench swe's)."""
    out = []
    for d in sorted(glob.glob(os.path.join(core.state_dir(), "bench", "tbench", "*", "results.json"))):
        try:
            with open(d, encoding="utf-8") as f:
                r = json.load(f)
            row = {"suite": "tbench", "run": os.path.basename(os.path.dirname(d)), "model": r["model"],
                   "passed": r["passed"], "n": r["n"], "harnesses": r["harnesses"], "table": table(r)}
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append((row, f"{row['run']}  tbench  {row['model']}: {row['table']}"))
    return out


def cmd(args):
    from sampo import cli
    try:
        tasks = args.tasks or fetch_tasks()
        out = os.path.join(core.state_dir(), "bench", "tbench", time.strftime("%Y%m%d-%H%M%S"))
        hs = HARNESSES if args.harness == "both" else (args.harness,)
        res = run_bench(args.model, tasks, out, args.n, args.category, args.only, args.max_steps, hs)
    except Stop as e:
        raise cli.UsageError(f"bench tbench stopped: {e}")
    lines = [f"{res['model']}: {table(res)}  ({out})"] + \
        [f"  {'PASS' if x['passed'] else 'FAIL'} {x['id']} [{x['harness']}] ({x['ended']}, {x['seconds']} s)"
         for x in res["tasks"]]
    if res["stopped"]:
        lines.append(f"stopped early: {res['stopped']}")
    return cli.out(args, dict(res, dir=out), "\n".join(lines))
