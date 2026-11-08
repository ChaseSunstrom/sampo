"""fm prove --infra (T-2385, Mission 7): the ecosystem's own validator on each infra file, on a throwaway copy, offline.

A Dockerfile, compose file, Terraform folder, Helm chart, Kubernetes manifest, CI workflow, shell script or systemd unit
is checked by the tool its ecosystem ships (hadolint, docker compose config, terraform validate, helm lint, kubeconform,
actionlint, shellcheck, systemd-analyze verify). A tool that fails fails the file; one that isn't on PATH leaves the file
unproven, names the tool and files one capability-miss task (fm plugins find); Sampo installs nothing, downloads nothing
and runs no verb that touches an account (the CLASSES table holds validate-only verbs; fmcore._LIVE and the guard stay the
boundary for what a session may run). Exit 0 none failed and one passed, 1 one failed, 2 nothing checked."""
import fnmatch
import glob
import os
import re
import shutil
import stat
import sys
import tempfile
import time

from sampo import core as c

INFRA_T, FINISH_T, INFRA_MAX = 300, 90, 200  # seconds for a whole run, for the one fm task finish makes, and files run (the rest: unproven)
STAGE_MAX = 200 << 20  # bytes one run copies; over it every file is unproven
SCHEMAS_ENV = "KUBECONFORM_SCHEMAS"  # kubeconform fetches schemas over the network unless it has a local folder
SCHEMA_PATH = "{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json"
NOT_INSTALLED = re.compile(r"is not a docker command|unknown command|unknown docker command")  # a plugin docker lacks
BENIGN = re.compile(r"is not executable|No such file or directory")  # systemd: a unit's ExecStart binary lives elsewhere
TF_UNINIT = re.compile(r"Missing required provider|Inconsistent dependency lock file|Failed to query available provider"
                       r"|Module not installed|Module source has changed|Required plugins are not installed")
REMOTE = re.compile(r"(?i)\b(?:oci|git|https?|ssh)(?:\+\w+)?://|\bgit@")  # a compose include compose would fetch
INCLUDE = re.compile(r"(?ms)^include:(.*?)(?=^[^\s#-]|\Z)")
HELM_PARTS = {"Chart.yaml", "values.yaml", "values.schema.json", "templates", "crds"}
GONE = {".git", "node_modules", "target", "__pycache__", ".venv", "venv", ".terraform"}
BAD = {"fail": 1, "pass": 0, "unchecked": 2}

# One class per ecosystem, first match wins for a file. card: the stack card whose detect globs name its files (skip:
# globs of that card's that aren't the validator's, e.g. .dockerignore); globs: its own where no card has them.
# unit: dir (one run per folder) or chart (one run per Chart.yaml's folder). {f}: the file, {d}: its folder, both
# relative to the copy's root; every argv is a check that reads files: no apply, plan, init, install, push or pull.
# Thresholds: a style or info note (and SC1091 on a sourced library, SC2148 on a file without a shebang) doesn't refuse a close.
CLASSES = {
    "dockerfile": {"card": "docker", "skip": (".dockerignore",), "tool": "hadolint",
                   "argv": ["hadolint", "--failure-threshold", "warning", "{f}"]},
    "compose": {"globs": ("docker-compose*.yml", "docker-compose*.yaml", "compose.yml", "compose.yaml", "compose.*.yml",
                          "compose.*.yaml"), "tool": "docker compose",
                "argv": ["docker", "compose", "-f", "{f}", "config", "-q"]},
    "terraform": {"card": "terraform", "skip": ("*.tfvars", ".terraform.lock.hcl"), "tool": "terraform validate",
                  "unit": "dir", "argv": ["terraform", "-chdir={d}", "validate", "-no-color"]},
    "helm": {"globs": ("Chart.yaml",), "tool": "helm lint", "unit": "chart", "argv": ["helm", "lint", "{d}"]},
    "kubernetes": {"card": "kubernetes", "tool": "kubeconform",
                   "argv": ["kubeconform", "-strict", "-ignore-missing-schemas", "-schema-location", "{schemas}", "{f}"]},
    "workflow": {"card": "github-actions", "skip": (".github/actions/*/action.yml",), "tool": "actionlint",
                 "argv": ["actionlint", "{f}"]},
    "shell": {"card": "bash", "skip": (".shellcheckrc", "*.bats"), "tool": "shellcheck",
              "argv": ["shellcheck", "-S", "warning", "-e", "SC1091,SC2148", "{f}"]},
    "systemd": {"card": "systemd", "tool": "systemd-analyze verify", "argv": ["systemd-analyze", "verify", "--man=no", "{f}"]},
}


def _signals(k):
    """[(kind, value)] from the class's card (its file: and ext: detect signals) and its own globs."""
    out = [("file", g) for g in k.get("globs", ())]
    if k.get("card"):
        from sampo import lib
        for path in glob.glob(os.path.join(lib.CARDS, "*", k["card"] + ".md")):
            try:
                with open(path, encoding="utf-8") as f:
                    detect = lib.parse(f.read()).get("detect") or []
            except OSError:
                continue
            out += [tuple(s.split(":", 1)) for s in detect if isinstance(s, str) and s.startswith(("file:", "ext:"))]
    return out


def _hit(sigs, rel):
    base, ext = os.path.basename(rel), os.path.splitext(rel)[1].lower()
    return any((kind == "ext" and ext == v.lower()) or (kind == "file" and fnmatch.fnmatchcase(rel if "/" in v else base, v))
               for kind, v in sigs)


def _regular(root, rel):
    try:
        return stat.S_ISREG(os.lstat(os.path.join(root, rel)).st_mode)
    except OSError:
        return False


def _listing(root):
    """The tree's files, root-relative and sorted: tracked and not-ignored, none inside .git, node_modules, .terraform
    and the like or a Claude worktree."""
    from sampo import lib
    files = sorted({f.replace(os.sep, "/") for f in lib._files(root)})
    return [f for f in files if not GONE & set(f.split("/")[:-1]) and not f.startswith(".claude/worktrees/")]


def classify(root, only=None, files=None):
    """[(rel, class)] sorted: the tree's infra files, each in the first class that takes it. only (a list of repo-relative
    paths, e.g. what a task touched) narrows to those files, a Helm chart to the one a touched part of it belongs to;
    only those (and the Chart.yaml above them) are matched against the globs. Symlinks are left out."""
    files = _listing(root) if files is None else files
    sel = None
    if only is not None:
        sel = {x.replace(os.sep, "/") for x in only}
        charts = {(d + "/" if d else "") + "Chart.yaml" for x in sel for d in _above(x)}
        keep = sel | charts
        files = [f for f in files if f in keep]
    sigs = {n: (_signals(k), _signals({"globs": k.get("skip", ())})) for n, k in CLASSES.items()}
    out = []
    for rel in files:
        name = next((n for n, (yes, no) in sigs.items() if _hit(yes, rel) and not (no and _hit(no, rel))), None)
        if not name or not _regular(root, rel):
            continue
        if sel is not None:
            if name == "helm":
                d = os.path.dirname(rel)
                under = [f[len(d) + 1:] if d else f for f in sel if not d or f.startswith(d + "/")]
                if not any(u.split("/")[0] in HELM_PARTS or u.startswith("values") for u in under):
                    continue
            elif rel not in sel:
                continue
        out.append((rel, name))
    return out


def _above(rel):
    """The folders above a file, deepest first, '' the root."""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


_OVERRIDE = re.compile(r"(docker-compose|compose)\.[^/]+\.ya?ml")
COMPOSE_UNSET = re.compile(r'The \\?"?(\w+)\\?"? variable is not set')  # compose's warning for a blank ${VAR}


def _compose_base(root, rel):
    """T-3897: the base compose file an override (docker-compose.gpu.yml, compose.prod.yaml) adds to, beside it in
    the same folder, or None: a base file, or an override with no base, is checked alone."""
    d, name = os.path.split(rel)
    m = _OVERRIDE.fullmatch(name)
    if not m:
        return None
    for ext in (".yml", ".yaml"):
        base = os.path.join(d, m.group(1) + ext)
        if os.path.isfile(os.path.join(root, base)) and not os.path.islink(os.path.join(root, base)):
            return base
    return None


def _which(name, env, root):
    """The tool's path on the user's PATH (env's), or None: a link into the tree is the tree's own file, not the user's tool."""
    exe = shutil.which(name, path=env["PATH"])
    if exe and os.path.commonpath([os.path.realpath(exe), root]) == root:
        return None
    return exe


def _first(text, n=3):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return c.fit(c.redact(c.plain(" | ".join(lines[:n]))), 240)


def _stage(root, rels, extra=()):
    """(tmp, dst): copies of the regular files in rels, and of those under each folder in extra (a Terraform folder's own
    .terraform). Never a symlink, a socket or .git; ValueError past STAGE_MAX bytes; a failed copy leaves nothing."""
    tmp = tempfile.mkdtemp(prefix="fm-prove-")
    dst, size = os.path.join(tmp, "w"), 0

    def put(rel):
        nonlocal size
        src = os.path.join(root, rel)
        st = os.lstat(src)
        if not stat.S_ISREG(st.st_mode):
            return
        size += st.st_size
        if size > STAGE_MAX:
            raise ValueError(f"the tree is over the {STAGE_MAX >> 20} MB copy cap")
        os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
        shutil.copy2(src, os.path.join(dst, rel))
    try:
        os.makedirs(dst)
        for rel in rels:
            put(rel)
        for d in extra:
            for top, dirs, names in os.walk(os.path.join(root, d)):
                dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(top, x))]
                for n in names:
                    put(os.path.relpath(os.path.join(top, n), root))
    except (OSError, ValueError):
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, dst


def _run(argv, cwd, env, end):
    """fmfind._run_tool with its timeout clamped to the time left; (out, err, rc, why), why also when it couldn't start."""
    from sampo import find
    old = find.TOOL_T
    find.TOOL_T = max(1, min(old, int(end - time.monotonic())))  # ponytail: a module global, so one run at a time
    try:
        return find._run_tool(argv, cwd, env)
    except OSError as e:  # a bad shebang or a missing interpreter: Popen raises
        return "", "", None, f"couldn't start ({c.fit(c.plain(str(e)), 100)})"
    finally:
        find.TOOL_T = old


def sandbox_prefix(env, root, cwd, end):
    """`unshare -r -n` (no network) as an argv prefix when it works unprivileged here, else [] (silently plain). T-2386:
    shared with fm prove --migration."""
    exe = _which("unshare", env, root)
    ok = exe and _run([exe, "-r", "-n", sys.executable, "-c", "pass"], cwd, env, end)[2:] == (0, None)
    return [exe, "-r", "-n"] if ok else []


def touched(p, b):
    """The files the task b changed, relative to the project p's folder (git names them from the repo's top; the project
    may be a folder in it)."""
    from sampo import map as smap
    top = c.git_root(p.root)
    base = c.task_base(p.root, b) if top else None
    prefix = os.path.relpath(p.root, top).replace(os.sep, "/") if top else "."
    return [f[len(prefix) + 1:] if prefix != "." and f.startswith(prefix + "/") else f
            for f in (smap.changed(p.root, base) if base else [])]


def infra(root, allowed=False, p=None, only=None, budget=None):
    """{"root", "files": [{"file", "class", "tool", "verdict": pass|fail|unproven, "why"}], "verdict": fail|pass|unchecked}.
    allowed: the tree is trusted (terraform validate loads the folder's own provider plugins, the tree's code, so it
    runs only then). p: a Sampo project that gets one capability-miss task per missing tool. Nothing runs outside a
    throwaway copy of root; the environment is Sampo's minimal one (no credentials), HOME the copy's folder; the
    tool runs under `unshare -r -n` (no network) when that works unprivileged here, plain when it doesn't."""
    from sampo import find
    root = os.path.realpath(root)
    env, end = find._tool_env(root), time.monotonic() + (INFRA_T if budget is None else budget)
    units, rows, held, net = {}, [], [], []
    files = _listing(root)
    found = classify(root, only, files)

    def stage():
        if not held:
            tf = sorted({os.path.dirname(rel) for rel, n in found if n == "terraform"})
            extra = [(d + "/" if d else "") + ".terraform" for d in tf if allowed
                     and os.path.isdir(os.path.join(root, d, ".terraform"))
                     and not os.path.islink(os.path.join(root, d, ".terraform"))]
            held.append(_stage(root, files, extra))  # ponytail: every listed file; a path list when copies hurt
        return held[0]

    def sandbox(dst):
        if not net:
            net.append(sandbox_prefix(env, root, dst, end))
        return net[0]

    def miss(name):
        if p:
            from sampo import eco
            eco.capture_once(p, f"Missing tool {name}: find it (fm plugins find {name}) or build it (script + test)\n"
                               f"capability-miss: {name}", "FEATURE", "self", key=f"capability-miss: {name}")

    def check(rel, name, k, n):
        d = os.path.dirname(rel) or "."
        exe = _which(k["argv"][0], env, root)
        if not exe:
            miss(k["argv"][0])
            return "unproven", f"{k['argv'][0]} is not on PATH (Sampo installs nothing: fm plugins find {k['argv'][0]})"
        schemas = os.environ.get(SCHEMAS_ENV, "")
        if name == "terraform":
            if not os.path.isdir(os.path.join(root, d, ".terraform")) or os.path.islink(os.path.join(root, d, ".terraform")):
                return "unproven", "the folder isn't initialised (terraform init downloads providers: run it yourself first)"
            if not allowed:
                return "unproven", ("terraform validate loads the folder's own provider plugins, the tree's code: "
                                    "fm prove --yes-run PATH says yes to the tree")
        if name in ("helm", "compose"):
            try:
                with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
                    text = f.read(1 << 20)
            except OSError:
                text = ""
            if name == "helm" and re.search(r"(?m)^dependencies:", text) and not os.path.isdir(os.path.join(root, d, "charts")):
                return "unproven", "the chart's dependencies aren't vendored (helm dependency build needs the network)"
            inc = INCLUDE.search(text) if name == "compose" else None
            if inc and REMOTE.search(inc.group(1)):
                return "unproven", "an include: names a remote source (oci, git, http); compose would fetch it"
        if name == "kubernetes" and not (os.path.isabs(schemas) and os.path.isdir(schemas)):
            return "unproven", (f"kubeconform fetches schemas over the network unless it has a local folder: set "
                                f"{SCHEMAS_ENV}=DIR to an absolute path (yannh/kubernetes-json-schema layout)")
        if n >= INFRA_MAX:
            return "unproven", f"over the {INFRA_MAX}-file cap of one run"
        if time.monotonic() >= end:
            return "unproven", "time budget spent"
        try:
            tmp, dst = stage()
        except (OSError, ValueError) as e:
            return "unproven", f"couldn't copy the tree: {c.fit(c.plain(str(e)), 120)}"
        safe = lambda x: "./" + x if x.startswith("-") else x  # noqa: E731  a file or folder named -x is not an option
        argv = sandbox(dst) + [exe] + [a.replace("{f}", safe(rel)).replace("{d}", safe(d))
                                       .replace("{schemas}", os.path.join(schemas, SCHEMA_PATH)) for a in k["argv"][1:]]
        base = _compose_base(root, rel) if name == "compose" else None
        if base:  # T-3897: an override (docker-compose.gpu.yml) only adds to its base: compose reads them together
            i = argv.index("-f", len(sandbox(dst)))
            argv[i:i] = ["-f", safe(base)]
        out, err, rc, why = _run(argv, dst, dict(env, HOME=tmp, NO_COLOR="1", CHECKPOINT_DISABLE="1", TF_IN_AUTOMATION="1"), end)
        if why:
            return "unproven", f"{k['tool']} {why}"
        if rc == 0:
            return "pass", ""
        if NOT_INSTALLED.search(err):
            miss(k["tool"])
            return "unproven", f"{k['tool']} isn't installed ({_first(err, 1)})"
        text = (out + "\n" + err).replace(dst + os.sep, "").replace(tmp, "")
        if name == "terraform" and TF_UNINIT.search(text):
            return "unproven", "the folder's providers or modules aren't installed here (terraform init is yours to run)"
        unset = sorted(set(COMPOSE_UNSET.findall(text))) if name == "compose" else []
        if unset:  # T-3897: the copy leaves the .env out (secrets stay home), so a ${VAR} reads blank: no verdict
            return "unproven", (f"it reads {c.joinmore(unset, 4)} from an .env the copy leaves out (secrets stay "
                                f"home); with them blank compose can't judge it")
        lines = [l for l in text.splitlines() if l.strip()]
        bad = "\n".join(l for l in lines if not (name == "systemd" and BENIGN.search(l)))
        if name == "systemd" and lines and not bad:
            return "pass", ""  # only an ExecStart binary that lives elsewhere; a silent exit 1 is still a failure
        return "fail", f"exit {rc}" + (f": {_first(bad)}" if bad else " with no output")

    try:
        for rel, name in found:
            k = CLASSES[name]
            key = (name, os.path.dirname(rel)) if k.get("unit") else (name, rel)
            if key not in units:
                units[key] = check(rel, name, k, len(units))
            rows.append({"file": rel, "class": name, "tool": k["tool"], "verdict": units[key][0], "why": units[key][1]})
    finally:
        if held:
            shutil.rmtree(held[0][0], ignore_errors=True)
    got = {x["verdict"] for x in rows}
    return {"root": root, "files": rows, "verdict": "fail" if "fail" in got else "pass" if "pass" in got else "unchecked"}


def render(r):
    lines = [f"{x['verdict']:<8} {c.fit(c.plain(x['file']), 100)} [{x['tool']}]" + (f": {x['why']}" if x["why"] else "")
             for x in r["files"]]
    n = lambda v: sum(1 for x in r["files"] if x["verdict"] == v)  # noqa: E731
    return lines + [f"infra: {n('pass')} passed, {n('fail')} failed, {n('unproven')} unproven"
                    if r["files"] else "infra: no Dockerfile, compose, Terraform, Helm, Kubernetes, workflow, shell or "
                    "systemd file here"]


def cmd_infra(args, root):
    """fm prove --infra [PATH]: exit 0 none failed (one passed), 1 one failed, 2 nothing checked."""
    from sampo import prove
    if args.yes_run:
        try:
            prove.remember(root)
        except OSError as e:
            print(f"fm prove: ran on your yes, but couldn't keep it for {root}: {c.plain(str(e))[:300]}", file=sys.stderr)
    p = c.find_project(os.getcwd())
    r = infra(root, args.yes_run or prove.trusted(root) or prove._lane_trusted(p, root), p=p)
    from sampo import cli
    cli.out(args, r, "\n".join(render(r)))
    return BAD[r["verdict"]]


def for_task(p, b, say=print):
    """(code, text) for fm task finish, or None: the infra files the task touched, proved. A missing validator is said
    (and filed once as a capability-miss), never installed; all-unproven records no run. A failing file refuses the close
    (text: every file's line, the failing ones first-class in the refusal)."""
    from sampo import prove
    files = touched(p, b)
    r = infra(p.root, prove.trusted(p.root) or prove._lane_trusted(p, p.root), p=p, only=files, budget=FINISH_T) \
        if files else None
    if not r or not r["files"]:
        return None
    lines = render(r)
    for x, line in zip(r["files"], lines):
        if x["verdict"] == "unproven":
            say("infra: " + line)
    return None if r["verdict"] == "unchecked" else (BAD[r["verdict"]], "\n".join(lines))
