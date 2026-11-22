"""fm smoke (T-0417): the product checked the way its user uses it. JARVIS closed 34 tasks with its tests green while
two panels of its web UI never loaded; nothing Sampo ran had ever opened the page. `fm smoke set web <url>` records
the address and makes `fm smoke` one of the project's gates; `fm smoke` opens it in a headless browser (the project's
own Playwright) at desktop and phone width, clicks every tab, and names each defect a user would see.

T-1509: it also shoots the start page's frame matrix (ui/visual-qa.md), and `fm smoke --profile NAME` crawls under a
hostile condition (PROFILES); their findings fail it only when strict. Screenshots land in <project state>/smoke/
<commit>[-dirty-<diff hash>]/<default|profile>/ under the views' own names, so a receipt can point at them; a second run
on the same tree waits for the first, then replaces its folder."""
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile

from sampo import core as c
from sampo import killsafe

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "smoke_web.cjs")
GATE = "fm smoke"
KEEP = 5  # commits whose screenshots are kept per project
LOCK_S = 300  # a run waits this long for another on its commit folder (a crawl's 240 s, then its kill)
PROFILES = {  # smoke_web.cjs applies each (its PROFILES has the same names, in this order)
    "torture": "90-letter words, right-to-left runs and 20-digit numbers written into the page's text",
    "empty": "every list in the JSON the app's own host sends emptied",
    "offline": "the network cut once the page has loaded",
    "slow3g": "2 s latency and 50 KB/s each way",
    "deuteranopia": "seen without green cones (a colour filter on the page)",
    "protanopia": "seen without red cones",
    "tritanopia": "seen without blue cones",
    "achromatopsia": "seen without colour",
    "frozen": "the clock fixed at 2026-01-15 10:00 UTC and animations paused",
    "motion": "five frames 250 ms apart per view",
}


def playwright(root):
    """The Playwright package a crawl can require: FOREMAN_PLAYWRIGHT, else the project's node_modules (a sub-app's
    too, as JARVIS keeps it in jarvis-web/), never a worktree's copy."""
    if os.environ.get("FOREMAN_PLAYWRIGHT"):
        return os.environ["FOREMAN_PLAYWRIGHT"]
    for depth in ("", "*/", "*/*/"):
        for d in sorted(glob.glob(os.path.join(root, depth + "node_modules/playwright"))):
            if ".claude/" not in os.path.relpath(d, root) and os.path.isfile(os.path.join(d, "package.json")):
                return d  # review: relative, so a project under ~/.claude still finds its own
    return None


def _runner():
    """FOREMAN_SMOKE_RUNNER stands in for the browser, only for a Sampo home in a temp folder (the tests'), so a
    command can't forge a passing gate with it (review)."""
    runner = os.environ.get("FOREMAN_SMOKE_RUNNER")
    tmp = os.path.realpath(tempfile.gettempdir()) + os.sep
    return runner if runner and os.path.realpath(c.foreman_home()).startswith(tmp) else None


def _clean(text):
    """Page text (console lines, alerts, tab names) reaches the terminal and the model: no control characters, capped."""
    return c.fit(re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(text)), 300)


def _kill(proc):
    """A crawl past its time: its process group, SIGTERM first (node then exits, and Playwright's exit handler kills
    the browser it started in a group of its own), SIGKILL for whatever is left (T-1509)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if not killsafe.killpg(proc.pid, sig):  # not a pid to signal: nothing to end
                return
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass


def crawl(pw, url, out, timeout=240, profile=""):
    """{"views": [...], "defects": [{"where", "what"}], "notes": [...]} from one crawl, or {"skipped": why} when the
    Playwright found has no browser installed."""
    os.makedirs(out, exist_ok=True)
    runner = _runner()
    cmd = (shlex.split(runner) + [url, out] if runner else ["node", SCRIPT, pw, url, out]) + ([profile] if profile else [])
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return {"views": [], "defects": [{"where": "smoke", "what": f"the crawl did not start: {e}"}]}
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _kill(proc)
        proc.communicate()
        return {"views": [], "defects": [{"where": "smoke", "what": f"the crawl did not finish: {e}"}]}
    try:
        res = json.loads(stdout.strip().splitlines()[-1])
        if not isinstance(res, dict):
            raise ValueError(res)
    except (ValueError, IndexError):
        return {"views": [], "defects": [{"where": "smoke", "what": "the crawl failed: "
                                          + _clean((stderr or stdout).strip())}]}
    if res.get("skipped"):
        return {"skipped": _clean(res["skipped"])}
    found = lambda key: [{"where": _clean(d.get("where", "?")), "what": _clean(d.get("what", "?"))}  # noqa: E731
                         for d in res.get(key) or [] if isinstance(d, dict)]
    return {"views": [_clean(v) for v in res.get("views") or []], "defects": found("defects"),
            "notes": [_clean(n) for n in res.get("notes") or []], "frame_findings": found("frame_findings"),
            # T-1627: the frozen profile's samples, for fm motion verify (which checks them) only
            "motion": res.get("motion") if isinstance(res.get("motion"), list) else [],
            "motion_notes": [_clean(n) for n in res.get("motion_notes") or [] if isinstance(n, str)][:20]}


def nudge(surfaces, meta):
    """fm doctor's line for a project that ships a web UI nobody checks (None when there's nothing to say)."""
    if "web UI" in surfaces and not meta.get("smoke"):  # {"web": url}, or {"off": why} for a UI no URL serves
        return "this project has a web UI and no product check: fm smoke set web <url>"
    return None


def cmd_smoke(args):
    from sampo import cli
    p = cli.resolve(args)
    profiles = list(PROFILES) if "all" in (args.profile or []) else list(dict.fromkeys(args.profile or []))
    if profiles and args.action == "set":
        raise cli.UsageError("--profile goes with fm smoke run")
    if args.action == "set" and args.words[:1] == ["off"]:  # a UI no URL serves (a plugin's own UI mod): no gate
        with c.lock(p.dir):
            meta = c.read_meta(p)
            meta["smoke"] = {"off": " ".join(args.words[1:]) or "no URL serves this UI"}
            meta["checks"] = [x for x in meta.get("checks") or [] if x != GATE]
            c.write_meta(p, meta)
        return cli.out(args, meta["smoke"], f"fm smoke is off here ({meta['smoke']['off']}).")
    if args.action == "set" and args.words[:1] == ["strict"]:  # review: beside meta["smoke"], whose keys are targets
        if args.words[1:] not in (["on"], ["off"]):
            raise cli.UsageError("fm smoke set strict on|off")
        with c.lock(p.dir):
            meta = c.read_meta(p)
            meta["smoke_strict"] = args.words[1] == "on"
            c.write_meta(p, meta)
        return cli.out(args, {"strict": meta["smoke_strict"]}, "fm smoke: frame findings and profile findings now "
                         + ("fail it." if meta["smoke_strict"] else "are reported, not failed."))
    if args.action == "set":
        kind, rest = (args.words[:1] or [""])[0], args.words[1:]
        url_ok = len(rest) == 1 and rest[0].startswith(("http://", "https://"))
        if kind == "web" and url_ok:
            entry = rest[0]
        elif kind == "http" and url_ok:  # T-0460: an API or service endpoint
            entry = {"url": rest[0], "status": args.status, "key": args.json_key}
        elif kind == "cmd" and rest:  # T-0460: a CLI, a daemon's status command, a voice pipeline's self-test
            entry = {"run": " ".join(rest), "expect": args.expect}
        else:
            raise cli.UsageError("fm smoke set web <http(s) url> | http <http(s) url> [--status N] [--json-key K] | "
                                   "cmd \"<command>\" [--expect REGEX] | strict on|off (or fm smoke set off <why>)")
        with c.lock(p.dir):
            meta = c.read_meta(p)
            meta["smoke"] = dict({k: v for k, v in (meta.get("smoke") or {}).items() if k != "off"}, **{kind: entry})
            meta["checks"] = list(meta.get("checks") or []) + ([GATE] if GATE not in (meta.get("checks") or []) else [])
            c.write_meta(p, meta)
        return cli.out(args, meta["smoke"], f"fm smoke checks the {kind} product check; it is one of this project's "
                                              f"gates (fm check runs it).")
    meta = c.read_meta(p)
    smoke, strict = meta.get("smoke") or {}, args.strict or bool(meta.get("smoke_strict"))
    url = smoke.get("web")
    if profiles and not url:
        raise cli.UsageError("a profile crawls the web UI: fm smoke set web <url> first")
    other = _other_checks(p, smoke, args)  # T-0460: before the browser, fast
    bad = [x for x in other if not x["ok"]]
    if not url:
        if not other:
            raise cli.UsageError("nothing to check yet: fm smoke set web <url> | http <url> | cmd \"<command>\"")
        c.log_event(p, "smoke", data={"checks": len(other), "defects": len(bad)}, session=cli.session())
        cli.out(args, {"checks": other}, "\n".join(f"{'ok' if x['ok'] else '✗ failed'}  {x['what']}: {x['detail']}"
                                                     for x in other))
        if bad:
            raise SystemExit(1)
        return None
    for x in other:
        print(f"{'ok' if x['ok'] else '✗ failed'}  {x['what']}: {x['detail']}")
    pw = playwright(p.root)
    skipped = None if pw or _runner() else ("no Playwright in this checkout (npm i -D playwright, then npx playwright "
                                            "install chromium; or FOREMAN_PLAYWRIGHT=<its package folder>)")
    runs, rev = os.path.join(p.dir, "smoke"), _rev(p.root)
    done, texts = [], []
    for name in ([] if skipped else profiles or [""]):
        out = os.path.join(runs, rev, name or "default")
        with c.lock(os.path.dirname(out), timeout=LOCK_S):  # review: a second run on the commit waits, then replaces
            shutil.rmtree(out, ignore_errors=True)  # no shot left from the run before
            res = crawl(pw, url, out, profile=name)
            if res.get("skipped"):  # no browser for one profile is none for the next
                shutil.rmtree(out, ignore_errors=True)
                skipped = f"{res['skipped']} (npx playwright install chromium installs it)"
                break
        # review: the frame matrix's findings (a live page still moving, a 768 overflow) are notes unless strict, so a
        # polling panel or a video doesn't break an existing gate, nor count against a closed task in fm outcomes
        findings = res.get("frame_findings") or []
        defects = (res.get("defects") or []) + (findings if strict else [])
        notes = (res.get("notes") or []) + ([] if strict else [f"{d['where']}: {d['what']}" for d in findings])
        views = res.get("views") or []
        c.log_event(p, "smoke_profile" if name else "smoke", session=cli.session(),  # a profile's finds: no regression
                    data=dict({"url": url, "views": len(views), "defects": len(defects)}, **({"profile": name} if name else {})))
        frames = os.path.join(out, "frames")
        texts.append(f"fm smoke{f' --profile {name}' if name else ''}: {url}: {len(views)} views, "
                     + (f"{len(defects)} defects:\n" + "\n".join(f"  ✗ {d['where']}: {d['what']}" for d in defects)
                        if defects else "no defects.")
                     + "".join(f"\n  · {n}" for n in notes)
                     + (f"\nThe page didn't load: start it, or fm smoke set off <why> to stop checking it."
                        if any(d["what"].startswith("did not load") for d in defects) else "")
                     + f"\nScreenshots: {out}"
                     + (f"\nFrames: {frames} (360, 768 and 1440 wide × light and dark, reduced motion at 360)"
                        if not name and os.path.isdir(frames) else ""))
        res = dict({k: v for k, v in res.items() if not k.startswith("motion")}, defects=defects, notes=notes,
                   frame_findings=findings)  # the motion samples are fm motion verify's, not this gate's
        done.append(dict(res, profile=name, shots=out) if name else dict(res, url=url, shots=out, frames=frames))
    for old in sorted(glob.glob(os.path.join(runs, "*")), key=_mtime)[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    if len(done) > 1:
        texts.append("Profiles: " + " · ".join(f"{r['profile']} ✗ {len(r['defects'])}" if r.get("defects")
                                               else f"{r['profile']} ✓" for r in done))
    if not strict and any(r["frame_findings"] or r.get("profile") and r["defects"] for r in done):
        texts.append("These findings are reported, not failed: --strict (or fm smoke set strict on) fails on them.")
    if skipped:  # review: a lane or machine without a browser isn't a product defect; say so, don't block (and what
        texts.append(f"fm smoke: skipped {url}: {skipped}.")  # earlier profiles found still shows)
    data = {"url": url, "rev": rev, "profiles": done} if profiles else done[0] if done else {"url": url}
    cli.out(args, dict(data, skipped=skipped) if skipped else data, "\n".join(texts))
    if bad or any(r["defects"] and (strict or not r.get("profile")) for r in done):
        raise SystemExit(1)


def _rev(root):
    """The folder a run's screenshots go in: the short commit; with tracked changes (fm sync's .sampo mirror aside),
    -dirty-<a hash of the diff>, so other changes never overwrite the frames a receipt names (-dirty alone when git
    can't say)."""
    sha = re.sub(r"\W", "", c._git(root, "rev-parse", "--short", "HEAD", timeout=10).strip())
    if not sha:
        return "no-commit"
    diff = c._git(root, "diff", "HEAD", "--binary", "--", ".", *(f":(exclude){m}" for m in c.MIRRORS), timeout=10, fail=None)
    if diff is None:
        return sha + "-dirty"
    return sha + (f"-dirty-{hashlib.sha256(diff.encode('utf-8', 'replace')).hexdigest()[:8]}" if diff else "")


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:  # another run pruned it first
        return 0


def _other_checks(p, smoke, args):
    """T-0460: the command and HTTP product checks: [{what, ok, detail}]. Only the URLs and commands the project set."""
    import urllib.request
    out = []
    cmd = smoke.get("cmd")
    if isinstance(cmd, dict) and cmd.get("run"):
        from sampo import cli
        cli.guard_stored(p, None, cmd["run"])  # T-0901 review: fm smoke set cmd stores it
        code, output = c.run_command(p.root, cmd["run"], 300)
        ok = not code and (not cmd.get("expect") or bool(re.search(cmd["expect"], output)))
        out.append({"what": f"cmd {cmd['run'][:80]}", "ok": ok, "detail": c.run_result(code, output)
                    + ("" if ok or code else f" (no match for {cmd['expect']!r})")})
    http = smoke.get("http")
    if isinstance(http, dict) and http.get("url"):
        try:
            with urllib.request.urlopen(http["url"], timeout=30) as r:  # noqa: S310 — the project's own URL
                status, body = r.status, r.read(1_000_000)
        except Exception as e:  # urllib's HTTPError carries the status
            status, body = getattr(e, "code", None), b""
        why = []
        if status != (http.get("status") or 200):
            why.append(f"status {status}, wanted {http.get('status') or 200}")
        if http.get("key") and status == (http.get("status") or 200):
            try:
                data = json.loads(body.decode("utf-8", "replace"))
                if not (isinstance(data, dict) and http["key"] in data):
                    why.append(f"JSON key {http['key']!r} missing")
            except ValueError:
                why.append("not JSON")
        out.append({"what": f"http {http['url'][:80]}", "ok": not why, "detail": "; ".join(why) or f"status {status}"})
    return out
