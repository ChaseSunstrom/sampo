"""T-0189: the guard replayed on real commands. Every shell command Claude ran in recent Claude Code sessions (their logs
under ~/.claude/projects) goes through the current guard, with no grants, in the folder it ran in, and the verdicts are
compared with the last accepted ones: a guard change shows which real commands it now blocks (a likely false positive)
or now lets through (a possible bypass). The baseline keeps a hash and a verdict per command, never the command."""
import glob
import hashlib
import json
import os
import re
import time

from sampo import core as c

DAYS = 14  # how far back the session logs are read
MAX = 5000  # distinct commands, newest sessions first
SHOW = 10  # changed commands listed per kind


def _logs(days):
    from sampo import claude  # review (T-1187): CLAUDE_CONFIG_DIR like the rest of fm, not a hardcoded ~/.claude
    root = os.path.join(claude.config_dir(), "projects")
    since = time.time() - days * 86400
    files = [f for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True) if os.path.getmtime(f) >= since]
    return sorted(files, key=os.path.getmtime, reverse=True)


def corpus(days=DAYS, cap=MAX):
    """[(command, cwd)], distinct, from the Bash tool calls in recent session logs."""
    seen, out = set(), []
    for path in _logs(days):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"Bash"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
                    for block in content if isinstance(content, list) else []:
                        cmd = (block.get("input") or {}).get("command") if isinstance(block, dict) and \
                            block.get("type") == "tool_use" and block.get("name") == "Bash" else None
                        key = (cmd, entry.get("cwd") or "")
                        if isinstance(cmd, str) and cmd.strip() and key not in seen:
                            seen.add(key)
                            out.append(key)
                            if len(out) >= cap:
                                return out
        except OSError:
            continue
    return out


def events(days=DAYS):
    """T-1058: every tool call in recent session logs as {session, text (what it did), result (its output), errored}."""
    for path in _logs(days):
        calls, order = {}, []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
                    for b in content if isinstance(content, list) else []:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                            text = " ".join(str(v) for v in b["input"].values())
                            calls[b.get("id")] = {"session": path, "text": text, "result": "", "errored": False}
                            order.append(b.get("id"))
                        elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                            r = b.get("content")
                            calls[b["tool_use_id"]].update(
                                result=r if isinstance(r, str) else json.dumps(r), errored=bool(b.get("is_error")))
        except OSError:
            continue
        for i in order:
            yield calls[i]


# a refusal as Claude Code records it: the hook error prefix ("PreToolUse:Bash hook error: "), then the guard's text
_REFUSAL = re.compile(r"(?:[\w:]+ hook error: )?(?:Ward|Foreman guard): blocked ([\w-]+): ([^.]*)")
MINE_CAP = 500  # refusals read, newest files first
MINE_SHOW = 5  # examples per rule in the text report


def _result_text(r):
    if isinstance(r, list):
        r = " ".join(b.get("text", "") for b in r if isinstance(b, dict))
    return r if isinstance(r, str) else ""


def mine(days=7):
    """T-1187: [{command, cwd, rule, message, label, session}] for each guard refusal in this project's transcripts
    (a Bash call whose error result opens with the guard's wording; text that merely quotes it is ignored). label,
    from the session's next Bash call: retried-unchanged, rewritten-not-refused, rewritten-refused or abandoned.
    ponytail: 'next call' stands in for 'the retry'; an unrelated next command reads as a rewrite."""
    from sampo import claude
    import fmguard as g
    home = os.path.expanduser("~")
    root = os.path.realpath(g.project_root_for(os.getcwd(), home) or os.getcwd())  # None at $HOME or /
    root = c.main_worktree(root) or root  # T-1203: a builder's linked worktree: the sessions are the main checkout's
    # Claude Code names the folder after the session's cwd (root, a subdirectory, a worktree under it; a long name gets a
    # hash suffix), so every folder starting with the root's name is a candidate and the calls' own cwd decides
    folder = os.path.join(claude.config_dir(), "projects", re.sub(r"[^A-Za-z0-9]", "-", root))
    since = time.time() - days * 86400
    files = sorted((f for pat in (folder, folder + "-*") for f in glob.glob(os.path.join(pat, "*.jsonl"))
                    if os.path.getmtime(f) >= since), key=os.path.getmtime, reverse=True)
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since))
    found = []
    for path in files:
        calls, order, seen = {}, [], 0
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"Bash"' not in line and "is_error" not in line:  # as corpus() does: most lines are neither
                        continue
                    if seen >= MINE_CAP - len(found):
                        break
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
                    for b in content if isinstance(content, list) else []:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use" and b.get("name") == "Bash" and isinstance(b.get("input"), dict) \
                                and isinstance(b["input"].get("command"), str):
                            calls[b.get("id")] = {"command": b["input"]["command"], "cwd": entry.get("cwd") or "",
                                                  "ts": str(entry.get("timestamp") or ""), "msg": None,
                                                  "grp": (entry.get("message") or {}).get("id") or b.get("id")}
                            order.append(b.get("id"))
                        elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls and b.get("is_error"):
                            calls[b["tool_use_id"]]["msg"] = _result_text(b.get("content"))[:600]
                            seen += bool(_REFUSAL.match(calls[b["tool_use_id"]]["msg"].lstrip()))
        except OSError:
            continue
        seq = [calls[i] for i in order]
        for i, call in enumerate(seq):
            m = _REFUSAL.match((call["msg"] or "").lstrip())
            cwd = call["cwd"]
            if not m or (call["ts"] and call["ts"][:19] < cutoff) or \
                    (cwd and os.path.realpath(cwd) != root and not os.path.realpath(cwd).startswith(root + os.sep)):
                continue  # too old, or another project whose folder name merely starts like this one's
            nxt = next((n for n in seq[i + 1:] if n["grp"] != call["grp"]), None)  # parallel siblings aren't a reaction
            label = "abandoned" if nxt is None else "retried-unchanged" if nxt["command"] == call["command"] else \
                "rewritten-refused" if _REFUSAL.match((nxt["msg"] or "").lstrip()) else "rewritten-not-refused"
            found.append({"command": c.redact(call["command"]), "cwd": call["cwd"], "rule": g.rule_id(m[1], m[2]),
                          "message": c.redact(call["msg"]), "label": label, "session": os.path.basename(path)[:-6],
                          "_raw": call["command"]})
            if len(found) >= MINE_CAP:
                break
        if len(found) >= MINE_CAP:
            break
    today = verdicts([(r["_raw"], r["cwd"]) for r in found])
    for r in found:
        r["today"] = today[_key(r.pop("_raw"), r["cwd"] or home)]
    return found


def cmd_mine(args, cli):
    args.days = min(max(args.days, 1), 365)
    found = mine(args.days)
    rules = {}
    for r in found:
        rules[r["rule"].split(":")[0]] = rules.get(r["rule"].split(":")[0], 0) + 1
    if not found:
        text = f"No guard refusals in this project's transcripts in the last {args.days} days."
    else:
        lines = [f"{len(found)} guard refusals in the last {args.days} days (newest transcripts first)"]
        by = {}
        for r in found:
            by.setdefault(r["rule"], []).append(r)
        for rule, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"{rule}  x{len(rs)}")
            for r in rs[:MINE_SHOW]:
                lines.append(f"  {r['label']}; today {r['today']}: {c.fit(c.plain(r['command'].replace(chr(10), ' ⏎ ')), 400)}")
        text = "\n".join(lines)
    cli.out(args, {"days": args.days, "rules": rules, "refusals": found}, text)


def _key(cmd, cwd):
    return hashlib.sha256(f"{cwd}\0{cmd}".encode("utf-8", "replace")).hexdigest()[:20]


def _ctx(cwd, g=None):
    """The guard's context with no grants, in the folder a command runs in."""
    if g is None:
        import fmguard as g
    home = os.path.expanduser("~")
    cwd = cwd or home
    return g.Ctx(cwd=cwd, project_root=g.project_root_for(cwd, home), home=home, foreman_home=c.foreman_home(),
                 state_dir=c.state_dir(), state_fallbacks=c.state_fallbacks(), scratch=["/tmp", "/var/tmp"])


def verdicts(cmds, g=None):
    """{key: the category the guard blocks it with, or 'allow'} with no grants, each in the folder it ran in; g: another
    guard module (a candidate, T-0712)."""
    if g is None:
        import fmguard as g
    import warnings
    warnings.simplefilter("ignore", SyntaxWarning)  # the guard parses Python inside commands; their escapes aren't ours
    import tempfile
    home, out, was = os.path.expanduser("~"), {}, tempfile.tempdir
    tempfile.tempdir = "/tmp"  # T-1291: the corpus is labelled on Linux's layout; macOS's /var/folders/… must judge the same
    # T-1599: the guard reads python's own settings from its environment (an inherited PYTHONPATH makes every python -c
    # suspect); a replay judges as the hook does, not as the shell that runs the replay happens to be set up
    py = {k: os.environ.pop(k) for k in getattr(g, "_PY_PATH_VARS", ()) if k in os.environ}
    try:
        for cmd, cwd in cmds:
            cwd = cwd or home
            try:
                b = g.check("Bash", {"command": cmd}, _ctx(cwd, g))
                out[_key(cmd, cwd)] = b.category if b else "allow"
            except Exception as e:  # the guard fails closed in the hook; here it is a finding of its own
                out[_key(cmd, cwd)] = f"error: {type(e).__name__}"
    finally:
        tempfile.tempdir = was
        os.environ.update(py)
    return out


def mutate(cmds):
    """T-1066: drop one guard rule (category) at a time: a mutant turns that category's verdicts to 'allow'. A rule some
    command is blocked with is 'caught' (a case would notice its loss); one none is blocked with is 'untested'.
    ponytail: a rule is a category, so two rules sharing one category are one mutant."""
    import fmguard as g
    seen = set(verdicts(cmds).values())
    return ([x for x in g.CATEGORIES if x in seen], [x for x in g.CATEGORIES if x not in seen])


def _checkout_plugin(cwd):
    """The plugin/ of the Sampo checkout cwd is in (a builder's worktree included), or None outside one."""
    d = os.path.abspath(cwd)
    while True:
        if os.path.isfile(os.path.join(d, "plugin", "lib", "fmguard.py")):
            return os.path.join(d, "plugin")
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def mutate_extras(plugin_root):
    """[(source, path in the copy)]: files beside the plugin tree that the guard's tests import (T-1605: the evasion
    corpus's generator reads site/bench_safety.py), so a mutant copy of plugin/ can run them."""
    src = os.path.join(os.path.dirname(os.path.abspath(plugin_root)), "site", "bench_safety.py")
    return [(src, os.path.join("site", "bench_safety.py"))] if os.path.isfile(src) else []


def _cmd_flip_module(args, cli, root):
    """T-3273: fm bench guard --mutate --module X --flip [--func F]: flip comparisons, booleans and constants in any module
    (every function, or F) and run only the tests that import it (or --tests) at idle priority; survivors are flips no
    test notices, and the exit is 1 when there are any."""
    import shlex
    import sys
    from sampo import modmutate as mm
    module = os.path.abspath(args.module)
    try:
        name = mm.module_name(module, root)
    except ValueError as e:
        raise cli.UsageError(str(e))
    if args.tests:
        cmd = shlex.split(args.tests)
    else:
        found = mm.tests_for(module, root)
        if not found:
            raise cli.UsageError(f"no test under {os.path.join(root, 'tests')} imports {name}: name one with --tests")
        cmd = mm.test_cmd([f[:-len(".py")] for f in found])  # the runner takes test names, not files
    rel = os.path.join("plugin", os.path.relpath(module, root))
    try:
        res = mm.run(module, rel, root, "plugin", cmd, func=args.func, limit=args.max, timeout=args.timeout,
                     log=lambda m: print(m, file=sys.stderr, flush=True))
    except (ValueError, OSError, SyntaxError) as e:
        raise cli.UsageError(str(e))
    s = res["survived"]
    where = f"function {args.func}" if args.func else "every function"
    lines = [f"Flips in {name} ({where}): {res['tried']} of {res['total']} mutants, {res['killed']} killed, "
             f"{len(s)} survived"]
    lines += [f"  survived: {os.path.basename(module)}:{m['line']} {c.fit(m['stmt'], 110)}" for m in s]
    cli.out(args, {**res, "survivors": len(s)}, "\n".join(lines))
    if s:
        raise SystemExit(1)


def cmd_mutate(args, cli):
    """Drop (or --weaken) one rule site of the guard at a time and run its tests on each; survivors are untested rules."""
    import shlex
    import shutil
    import sys
    import fmguard as g
    from sampo import mutate as smutate
    if getattr(args, "project", None):
        cli.resolve(args)  # -p is checked like the other bench commands
    root = _checkout_plugin(os.getcwd()) or c.PLUGIN_ROOT  # T-1121: a lane mutates its own guard, not the main one's
    # T-3273: --module X --flip on any module but the guard flips every function of X (--func: one) against its tests
    flipped = (bool(args.module) and getattr(args, "flip", False)
               and os.path.abspath(args.module) != os.path.join(root, "lib", "fmguard.py"))
    if getattr(args, "func", None) and not flipped:
        raise cli.UsageError("--func needs --module and --flip (not fmguard.py): it names the function whose comparisons, "
                             "booleans and constants flip")
    if flipped:
        return _cmd_flip_module(args, cli, root)
    module = args.module or os.path.join(root, "lib", "fmguard.py")
    rel = os.path.join("plugin", "lib", "fmguard.py") if not args.module else os.path.join("plugin", "lib", os.path.basename(module))
    cmd = shlex.split(args.tests) if args.tests else [sys.executable, os.path.join("plugin", "tests", "run.py"), "-p", "test_guard*.py"]
    if not shutil.which("bwrap"):
        print("warning: bwrap is missing, so test_guard_diff's GuardAgainstBash is skipped and its rules look untested",
              file=sys.stderr)
    try:
        res = smutate.run(module, rel, root, "plugin", cmd, g.CATEGORIES, weaken=args.weaken,
                           flip=getattr(args, "flip", False),
                           only=args.only, limit=args.max, timeout=args.timeout, extra=mutate_extras(root),
                           log=lambda m: print(m, file=sys.stderr, flush=True))
    except (ValueError, OSError, SyntaxError) as e:
        raise cli.UsageError(str(e))
    s = res["survived"]
    mode = "flip" if getattr(args, "flip", False) else "weaken" if args.weaken else "drop"
    lines = [f"Guard mutation ({mode}): {res['tried']} of {res['total']} mutants, "
             f"{res['killed']} killed, {len(s)} survived"]
    if res["not_mutated"]:
        lines.append("  not mutated (no rule site found): " + ", ".join(res["not_mutated"]))
    lines += [f"  survived: fmguard.py:{m['line']} {m['category']}: {c.fit(m['stmt'], 110)}" for m in s]
    cli.out(args, {**res, "survivors": len(s)}, "\n".join(lines))
    if s:
        raise SystemExit(1)


def cmd_guard(args):
    """fm bench guard --coverage: the rules no command notices; --mutate: the rules no test notices (wild C10)."""
    from sampo import cli
    if args.mutate:
        return cmd_mutate(args, cli)
    if not args.coverage:
        raise cli.UsageError("fm bench guard needs --coverage or --mutate")
    if args.cmds:
        try:
            with open(args.cmds, encoding="utf-8") as f:
                cmds = [(ln.strip(), os.getcwd()) for ln in f if ln.strip()]
        except OSError as e:
            raise cli.UsageError(f"--cmds {args.cmds}: {e}")
    else:
        cmds = corpus(args.days)
    caught, untested = mutate(cmds)
    lines = [f"Guard mutation on {len(cmds)} commands: {len(caught)} rules caught, {len(untested)} untested"]
    if untested:
        lines.append("  untested (no command is blocked by it): " + ", ".join(untested))
    cli.out(args, {"commands": len(cmds), "caught": caught, "untested": untested}, "\n".join(lines))
    if untested:
        raise SystemExit(1)


def _baseline_path():
    return os.path.join(c.state_dir(), "guard-replay.json")


def candidate(ref):
    """A candidate guard as a module: a file path, or a git ref of Sampo's own repo (its plugin/lib/fmguard.py)."""
    import importlib.util
    import subprocess
    import tempfile
    if os.path.isfile(ref):
        path = ref
    else:
        repo = os.path.dirname(c.PLUGIN_ROOT)
        src = subprocess.run(["git", "-C", repo, "show", f"{ref}:plugin/lib/fmguard.py"], capture_output=True, text=True)
        if src.returncode:
            raise ValueError(f"no plugin/lib/fmguard.py at {ref}: {src.stderr.strip()[:200]}")
        path = os.path.join(tempfile.mkdtemp(prefix="fm-cand-"), "fmguard_candidate.py")
        with open(path, "w") as f:
            f.write(src.stdout)
    spec = importlib.util.spec_from_file_location("fmguard_candidate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def score(args, cli):
    """T-0712: a candidate guard against the working one on the corpus: what it would loosen (allow where the working
    guard blocks: each one a possible bypass to review) and tighten (new refusals: friction). Exit 1 when it loosens, or
    when it can't judge a command (T-1517: a candidate whose check raises is unable, never a clean score)."""
    t0 = time.monotonic()
    try:
        cand = candidate(args.candidate)
    except (ValueError, OSError, SyntaxError) as e:
        raise cli.UsageError(f"candidate {args.candidate}: {e}")
    cmds = corpus(args.days)
    now, new = verdicts(cmds), verdicts(cmds, cand)
    by_key = {_key(cmd, cwd or os.path.expanduser("~")): cmd for cmd, cwd in cmds}
    unjudged = [k for k in now if unable(new.get(k))]
    loosened = [k for k in now if now[k] != "allow" and new.get(k) == "allow"]
    tightened = [k for k in now if now[k] == "allow" and new.get(k) not in (None, "allow") and not unable(new.get(k))]
    changed = [k for k in now if k not in loosened and k not in tightened and not unable(new.get(k))
               and new.get(k) != now[k]]
    show = lambda k: c.fit(c.redact(" ".join(by_key[k].split())), 140)
    lines = [f"Candidate {args.candidate} on {len(cmds)} real commands ({time.monotonic() - t0:.1f} s): "
             f"{len(loosened)} loosened, {len(tightened)} tightened, {len(changed)} recategorised, "
             f"{len(unjudged)} unable"]
    for title, keys in (("loosened (allowed now: review each as a possible bypass)", loosened),
                        ("tightened (new refusals: friction)", tightened),
                        ("unable (the candidate raised: it judged nothing there)", unjudged)):
        if keys:
            lines.append(f"  {title}:")
            lines += [f"    {now[k]} → {new[k]} · {show(k)}" for k in keys[:SHOW]]
    cli.out(args, {"commands": len(cmds), "loosened": len(loosened), "tightened": len(tightened),
                     "recategorised": len(changed), "unable": len(unjudged)}, "\n".join(lines))
    if loosened or unjudged:
        raise SystemExit(1)


def cases_path():
    return os.environ.get("FOREMAN_GUARD_CASES") or os.path.join(_plugin_here(), "tests", "fixtures", "guard_cases.jsonl")


def allow_path():
    """T-1517: the allow-corpus, everyday commands the guard must pass; replayed with the labelled cases."""
    return os.path.join(_plugin_here(), "tests", "fixtures", "guard_allow.jsonl")


ALLOW_MIN = 40  # an allow-corpus with fewer cases (missing, emptied, cut down) is a broken corpus, never a quiet pass


def _corpus_paths():
    """The files a replay holds the guard to: a caller's own case file alone, else the cases and the allow-corpus."""
    return [cases_path()] if os.environ.get("FOREMAN_GUARD_CASES") else [cases_path(), allow_path()]


def _where(k):
    """A case's command and the folder it runs in: two cases with these equal are one case, whatever their ids."""
    return k["cmd"], os.path.normpath(resolve_cwd(k.get("cwd") or ""))


def unable(v):
    """T-1517: a verdict the guard couldn't give (verdicts() caught it raising): never a pass, never a block."""
    return isinstance(v, str) and v.startswith("error:")


def _plugin_here():
    """T-1175: the plugin/ of the checkout cwd is in (a builder's worktree), else PLUGIN_ROOT's."""
    return _checkout_plugin(os.getcwd()) or c.PLUGIN_ROOT


def repo_root():
    return os.path.dirname(c.PLUGIN_ROOT)


def _corpus_root():
    return os.path.dirname(_plugin_here())  # the checkout whose corpus cases_path() is


def resolve_cwd(cwd):
    """A stored cwd back to a folder: relative ones are in Sampo's repo, '' is the home folder (the guard's default)."""
    return os.path.join(_corpus_root(), cwd) if cwd and not os.path.isabs(cwd) else cwd or os.path.expanduser("~")


def _store_cwd(cwd):
    """cwd as stored: relative to the repo root when inside it, so the committed corpus isn't machine-specific."""
    cwd, root = os.path.realpath(cwd), os.path.realpath(_corpus_root())
    if cwd == os.path.realpath(os.path.expanduser("~")):
        return ""  # the guard's default folder
    return os.path.relpath(cwd, root) if cwd == root or cwd.startswith(root + os.sep) else cwd


def load_cases(path=None):
    """T-1162: ([{id, cmd, cwd, should: allow|block, why}], broken): the labelled cases fm guard wrong stored, and how
    many lines were not a case (a conflict marker, a torn line, a missing key). A missing file is no cases. No path:
    every corpus file (_corpus_paths), one command and folder once; opposite labels for one are one broken line (both
    kept, so the replay shows the command), and so is an allow-corpus under ALLOW_MIN cases (missing or cut down)."""
    if not path:
        cases, broken, label = [], 0, {}
        for one in _corpus_paths():
            got, bad = load_cases(one)
            broken += bad
            if one == allow_path() and len(got) < ALLOW_MIN:
                broken += 1
            for k in got:
                w = _where(k)
                if w not in label:
                    label[w] = k["should"]
                    cases.append(k)
                elif label[w] != k["should"]:
                    broken += 1
                    cases.append(k)
        return cases, broken
    cases, broken = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except OSError:
        return [], 0
    for ln in lines:
        try:
            k = json.loads(ln)
            ok = isinstance(k, dict) and all(isinstance(k.get(f), str) for f in ("id", "cmd", "should")) \
                and k["should"] in ("allow", "block")
        except ValueError:
            ok = False
        if ok:
            cases.append(k)
        else:
            broken += 1
    return cases, broken


def drift(cases):
    """The cases whose label the current guard (no grants) disagrees with, each with its 'got' verdict: allow, block,
    or unable (T-1517: the guard raised, so nothing was judged; never a pass, whatever the label)."""
    now = verdicts([(k["cmd"], resolve_cwd(k.get("cwd") or "")) for k in cases])
    out = []
    for k in cases:
        v = now[_key(k["cmd"], resolve_cwd(k.get("cwd") or ""))]
        got = "allow" if v == "allow" else "unable" if unable(v) else "block"
        if got != k["should"]:
            out.append({**k, "got": got})
    return out


def add_case(cmd, should, cwd="", why=""):
    """Append one labelled case (redacted; a command that still holds a key-shaped literal is refused); the same
    command and folder is stored once. Returns the case."""
    from sampo import cli
    cmd = cmd.strip()
    if not cmd or should not in ("allow", "block"):
        raise cli.UsageError("fm guard wrong needs a command and --should allow|block")
    if c.redact(cmd) != cmd or c.redact(why) != why:
        raise cli.UsageError("that command holds a key-shaped literal: replace it with a placeholder before storing it")
    cwd = _store_cwd(cwd or os.getcwd())
    case = {"id": _key(cmd, cwd), "cmd": cmd, "cwd": cwd, "should": should, "why": why}
    here = _where(case)
    for path in _corpus_paths():  # T-1517: a label the allow-corpus contradicts is refused too
        for k in load_cases(path)[0]:
            if _where(k) == here:
                if k["should"] != should:
                    raise cli.UsageError(f"already stored as should {k['should']}: edit {path} to change a label")
                return k
    os.makedirs(os.path.dirname(cases_path()), exist_ok=True)
    c.append_line(cases_path(), json.dumps(case) + "\n")
    return case


STALE_DAYS = 7  # T-1401: an unattended fm code run or fm release --push refuses a corpus replay older than this
GATE_WINDOW = 90  # days of ledger read for it: older counts as none on record
REFRESH = "fm replay --cases"


def record(cases, drifted, broken):
    """T-1401: the corpus replay's result on the ledger of the project the corpus belongs to (Sampo's own), else
    cwd's; safety_gate reads it."""
    p = c.find_project(_corpus_root()) or c.find_project(os.getcwd())
    data = {"cases": cases, "drifted": drifted, "broken": broken}
    if os.environ.get("FOREMAN_GUARD_CASES"):  # a case file of the caller's own: recorded, never a pass
        data["substitute"] = True
    if p:
        c.log_event(p, "guard_corpus", data=data)


def safety_gate(p):
    """T-1401: why the guard can't be trusted unattended, or None: the newest guard corpus replay on p's or Sampo's
    own ledger is missing, red, empty or older than STALE_DAYS. Names the check, its date and the refresh."""
    since = c.iso(time.time() - GATE_WINDOW * 86400)
    seen, events = set(), []
    for q in (p, c.find_project(repo_root())):
        if q and q.dir not in seen:
            seen.add(q.dir)
            events += c.ledger_events(q, kinds=["guard_corpus"], since=since)
    events = [e for e in events if isinstance(e.get("data"), dict) and c.parse_ts(e.get("ts"))
              and not e["data"].get("substitute")]
    head = "the guard corpus replay (fm replay --cases)"
    if not events:
        return f"{head} has no result on record in the last {GATE_WINDOW} days: run {REFRESH}"
    e = max(reversed(events), key=lambda e: e["ts"])  # the same second: the one that landed last
    d, on = e["data"], e["ts"][:10]
    cases, drifted, broken = (d.get(k) if isinstance(d.get(k), int) else -1 for k in ("cases", "drifted", "broken"))
    if drifted or broken or cases < 1:
        return (f"{head} failed on {on}: {drifted} drifted, {broken} broken, {cases} cases: fix the guard or the "
                f"labels, then run {REFRESH}")
    age = c.age_days(e["ts"])
    if age > STALE_DAYS or age < 0:  # dated in the future: a skewed clock or a planted record
        return f"{head} last passed on {on}, {int(age)} days ago (older than {STALE_DAYS}): run {REFRESH}"
    return None


def enforce(p, by, waiver=None, attended=False):
    """T-1401: the safety gate at a verb's door. A problem refuses (UsageError), unless attended (a warning to show) or
    waived (--stale-ok REASON, logged on the ledger, never silent). Returns the note to show, or None."""
    from sampo import cli
    if waiver is not None and not waiver.strip():
        raise cli.UsageError("--stale-ok needs a reason: --stale-ok \"why running on this replay is fine\"")
    why = safety_gate(p)
    if not why:
        return None
    if waiver:
        c.log_event(p, "safety_waiver", data={"check": "guard corpus replay", "by": by, "reason": waiver[:500],
                                               "problem": why})
        return f"{why} (waived: {waiver[:200]}; logged)"
    if attended:  # on the ledger too: FOREMAN_CODE_ATTENDED set by an agent is never a silent pass
        c.log_event(p, "safety_warned", data={"check": "guard corpus replay", "by": by, "problem": why})
        return f"warning: {why}"
    raise cli.UsageError(f"{by} refuses: {why}; or --stale-ok REASON runs anyway (logged)")


def report_cases(args, cli):
    """fm replay --cases: the labelled cases against the current guard; --capture: one FIX per drifted case (night)."""
    cases, broken = load_cases()
    bad = drift(cases)
    record(len(cases), len(bad), broken)
    made = []
    if args.capture and bad:
        p = cli.resolve(args)
        with c.lock(p.dir):
            have = "".join(b.section("Raw request") for b in c.load_briefs(p, include_archive=True))
            for k in bad:
                if f"[guard-case {k['id']}]" in have:
                    continue  # captured before, open or closed: a label is one task
                shown = c.fit(" ".join(k["cmd"].split()), 100)
                b = cli._create(p, f"Guard verdict drifted from its label: should {k['should']}: {shown}", "FIX", "S",
                                  "captured", raw=f"[guard-case {k['id']}] fm replay --cases (T-1162): the guard now "
                                  + ("raises on" if k["got"] == "unable" else f"{k['got']}s")
                                  + f" `{shown}` in {k.get('cwd') or '~'}; the label says {k['should']}"
                                  f" ({k.get('why') or 'no reason given'}). Fix fmguard.py, or correct the label."
                                  + (" The label is allow but the guard blocks: do not loosen the guard without the "
                                     "user's yes." if k["should"] == "allow" and k["got"] == "block" else ""),
                                  source="discovered")
                c.log_event(p, "capture", task=b.id, data={"source": "discovered", "type": "FIX"})
                made.append(b.id)
        if made:
            c.regen_views(p)
    unable = sum(k["got"] == "unable" for k in bad)
    lines = [f"Guard cases: {len(cases)} labelled, {len(bad)} drifted"
             + (f" ({unable} unable to judge)" if unable else "")
             + (f", {broken} broken line(s) skipped" if broken else "")] + [
        f"  should {k['should']}, guard {k['got']}: {c.fit(c.redact(' '.join(k['cmd'].split())), 120)}" for k in bad[:SHOW]]
    cli.out(args, {"cases": len(cases), "drifted": len(bad), "unable": unable, "broken": broken, "captured": made},
              "\n".join(lines))
    if broken or bad and not args.capture:
        raise SystemExit(1)


def report_oracle(args, cli):
    """T-1553: each labelled case (or --cmd in --cwd) the guard lets through, run in a throwaway sandbox that records
    every program the shell would start instead of running it; a recorded command the guard refuses on its own is a
    line it misread. A sandbox that didn't start or a guard that raised is unable, and fails the run like a misread."""
    from sampo import oracle
    if not oracle.sandbox():
        return cli.out(args, {"oracle": None, "why": "no bwrap"}, "Guard oracle: not run, bwrap (bubblewrap) isn't "
                         "installed; it runs commands only inside that sandbox and nothing else is used")
    if args.cwd and not args.cmd:
        raise cli.UsageError("--cwd goes with --cmd: the folder that one command would run in")
    if args.cmd:
        cases, broken = [{"cmd": args.cmd, "cwd": os.path.abspath(args.cwd or os.getcwd())}], 0
    else:
        cases, broken = load_cases()
    found = oracle.disagreements([(k["cmd"], k.get("cwd") or "") for k in cases])
    found = [{**d, "cmd": c.redact(d["cmd"]), "ran": d["ran"] and [c.redact(a) for a in d["ran"]]} for d in found]
    unable = [d for d in found if d["verdict"].startswith("unable")]
    bad = [d for d in found if d not in unable]
    lines = [f"Guard oracle: {len(cases)} {'command' if args.cmd else 'labelled cases'} run with every program "
             f"recorded, not run · {len(bad)} misread" + (f" · {len(unable)} unable" if unable else "")
             + (f" · {broken} broken line(s) skipped" if broken else "")]
    lines += [f"  {c.fit(' '.join(d['cmd'].split()), 70)} runs {c.fit(' '.join(d['ran']), 60)}"
              f" → {d['verdict'].split(':')[0]}" for d in bad[:SHOW]]
    lines += [f"  {c.fit(' '.join(d['cmd'].split()), 70)} → {d['verdict']}" for d in unable[:SHOW]]
    cli.out(args, {"cases": len(cases), "broken": broken, "unable": len(unable), "disagreements": bad + unable},
              "\n".join(lines))
    if found or broken:
        raise SystemExit(1)


def cmd_replay(args):
    from sampo import cli
    if getattr(args, "oracle", False):
        return report_oracle(args, cli)
    if getattr(args, "cases", False):
        return report_cases(args, cli)
    if getattr(args, "candidate", None):
        return score(args, cli)
    if args.cwd and not args.cmd:  # T-0293: accepted and ignored was worse than refused
        raise cli.UsageError("--cwd goes with --cmd: the folder that one command would run in")
    if args.cmd:  # T-0287: one command's verdict, asked and never run (no grants: what the guard says by default)
        import fmguard as g
        b = g.check("Bash", {"command": args.cmd}, _ctx(os.path.abspath(args.cwd or os.getcwd())))
        return cli.out(args, {"verdict": b.category if b else "allow", "detail": b.detail if b else None},
                         f"blocked {b.category}: {c.plain(b.detail)}" if b else "allow")
    t0 = time.monotonic()
    cmds = corpus(args.days)
    now = verdicts(cmds)
    path = _baseline_path()
    try:
        with open(path, encoding="utf-8") as f:
            base = json.load(f)
    except (OSError, ValueError):
        base = {}
    old = base.get("verdicts") or {}
    by_key = {_key(cmd, cwd or os.path.expanduser("~")): (cmd, cwd) for cmd, cwd in cmds}
    unjudged = [k for k, v in now.items() if unable(v)]  # T-1517: the guard raised: its own bucket, never a baseline
    blocked = [k for k, v in now.items() if k in old and v != "allow" and old[k] != v and not unable(v)]
    allowed = [k for k, v in now.items() if k in old and v == "allow" and old[k] != "allow"]
    fresh = sum(1 for k in now if k not in old)
    if args.accept:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        judged = {k: v for k, v in now.items() if not unable(v)}
        c.write_atomic(path, json.dumps({"at": c.now(), "verdicts": {**old, **judged}}, sort_keys=True))
    res = {"commands": len(cmds), "since_days": args.days, "baseline": base.get("at"), "newly_blocked": len(blocked),
           "newly_allowed": len(allowed), "unable": len(unjudged), "new": fresh,
           "seconds": round(time.monotonic() - t0, 1), "accepted": bool(args.accept)}
    show = lambda k: c.fit(c.redact(" ".join(by_key[k][0].split())), 140)
    lines = [f"Guard replay: {len(cmds)} real commands from the last {args.days} days of sessions "
             f"({res['seconds']} s) · baseline {base.get('at') or 'none yet'}"]
    buckets = [("newly blocked (likely false positives)", blocked), ("newly allowed (possible bypasses)", allowed)]
    if unjudged:
        buckets.append(("unable (the guard raised: it judged nothing there; never accepted as the baseline)", unjudged))
    for title, keys in buckets:
        lines.append(f"  {len(keys)} {title}")
        lines += [f"    {old.get(k)} → {now[k]} · {show(k)}" for k in keys[:SHOW]]
        if len(keys) > SHOW:
            lines.append(f"    … {len(keys) - SHOW} more")
    lines.append(f"  {fresh} new to the corpus" + (" · accepted as the baseline" if args.accept else
                 " · fm replay --accept takes these verdicts as the baseline" if blocked or allowed or not old else ""))
    cases, broken = load_cases()  # T-1182: the gate holds the labelled cases too (fm guard wrong)
    bad = drift(cases)
    record(len(cases), len(bad), broken)
    res.update(cases=len(cases), drifted=len(bad), broken=broken)
    if cases or broken:
        lines.append(f"  guard cases: {len(cases)} labelled, {len(bad)} drifted"
                     + (f", {broken} broken line(s)" if broken else ""))
        lines += [f"    should {k['should']}, guard {k['got']}: {c.fit(c.redact(' '.join(k['cmd'].split())), 120)}"
                  for k in bad[:SHOW]]
    cli.out(args, res, "\n".join(lines))
    if ((blocked or allowed) and not args.accept) or unjudged or bad or broken:
        raise SystemExit(1)
