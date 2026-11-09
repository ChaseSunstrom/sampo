"""T-3281: sampo spar, the nightly hook fuzz. Mutated hook payloads (malformed, huge, unicode, raw, missing fields) run
through every hook event in a temp home until a time bound; each distinct traceback is one crash, and --capture files
each as a FIX whose replay line is `sampo spar --seed S --replay K`: fm regenerates the mutant, so nothing is quoted
and nothing (a NUL byte, a lone surrogate) is lost. A crash is a traceback the hook printed or logged, nothing else:
models advise, the hook decides."""
import hashlib
import json
import os
import random
import re
import secrets
import subprocess
import time

from sampo import core as c
from sampo import drill

HOOK_S = 10.0  # one hook's ceiling on an idle host; c.load_scaled stretches it on a busy one
TAIL = 2000  # stderr kept per crash
JSON_BODY = 500  # a crash body in --json is clipped to this many chars
MAX_FILED = 5  # new FIXes per run: a bad night files a few; the rest are not filed this run

# One payload per hook event, in the shape the hook reads. test_spar asserts this covers every hooks.HANDLERS key.
PAYLOADS = {
    "SessionStart": {"session_id": "spar", "source": "startup"},
    "UserPromptSubmit": {"session_id": "spar", "prompt": "fix the thing"},
    "PreToolUse": {"session_id": "spar", "tool_name": "Edit", "tool_input": {"file_path": "README.md"}},
    "PostToolUse": {"session_id": "spar", "tool_name": "Edit", "tool_input": {"file_path": "README.md"},
                    "tool_response": {"success": True}},
    "PostToolUseValidate": {"session_id": "spar", "tool_name": "Edit", "tool_input": {"file_path": "README.md"}},
    "PostToolUseFailure": {"session_id": "spar", "tool_name": "Bash", "tool_input": {"command": "false"},
                           "error": "exit 1"},
    "PreCompact": {"session_id": "spar", "trigger": "auto"},
    "Stop": {"session_id": "spar", "stop_hook_active": False},
    "TaskCompleted": {"session_id": "spar", "task_id": "T-0001"},
    "SubagentStart": {"session_id": "spar", "agent_type": "fm-builder"},
    "SubagentStartContext": {"session_id": "spar", "agent_type": "Explore"},
    "SubagentStop": {"session_id": "spar", "agent_type": "fm-builder", "stop_hook_active": False},
    "MessageDisplay": {"session_id": "spar", "message": "hello"},
    "Notification": {"session_id": "spar", "message": "waiting"},
    "SessionEnd": {"session_id": "spar", "reason": "other"},
    "PermissionRequest": {"session_id": "spar", "tool_name": "Bash", "tool_input": {"command": "ls"}},
}
SEEDS = tuple(PAYLOADS.items())  # (event, payload)


def mutate(seed, rng):
    """Yields (kind, body): the seed payload as JSON, then malformed text, a huge string, unicode read raw, a raw lone
    surrogate (invalid UTF-8 once encoded), and a missing field. The same rng state gives the same list."""
    _, payload = seed
    body = json.dumps(payload)
    yield "seed", body
    yield "malformed", body[:rng.randrange(1, max(2, len(body)))]  # cut mid-token: never valid JSON
    yield "malformed", body + "}" + rng.choice(['"', "\x00", ","])
    yield "huge", json.dumps(dict(payload, _spar="A" * rng.randrange(100_000, 200_000)))
    pick = rng.choice(["é中\U0001f525", "‮\x00​", "\U000103ff"])
    yield "unicode", json.dumps(dict(payload, _spar=pick), ensure_ascii=False)
    yield "raw", json.dumps(dict(payload, _spar="x\ud800y"), ensure_ascii=False)
    drop = rng.choice(sorted(payload)) if payload else None
    yield "missing", json.dumps({k: v for k, v in payload.items() if k != drop})


def _mutants(rng):
    """(event, kind, body) for every seed in turn, round after round: the run ends at the bound, not at the list's end."""
    while SEEDS:  # no seeds, no mutants: an empty loop would spin past the bound
        for event, payload in SEEDS:
            for kind, body in mutate((event, payload), rng):
                yield event, kind, body


def _day():
    """A drill day with a project and one focused task, so the hooks have state to read. A day that fails to set up is
    removed before the error goes up."""
    d = drill.Day()
    try:
        d.fm("init")
        d.fm("task", "new", "Spar target", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
    except BaseException:
        d.remove()
        raise
    return d


def _ceiling():
    """One hook's ceiling, scaled by the host's load: a busy machine is slow, not hung."""
    return c.load_scaled(HOOK_S)


def _exec(day, event, body, timeout):
    """One hook run in the day: (rc, stdout, stderr, timed_out). The body goes in as raw bytes, so a malformed one
    reaches the hook unchanged."""
    try:
        p = subprocess.run([os.path.join(c.PLUGIN_ROOT, "hooks", "hook"), event], cwd=day.repo, env=day.env,
                           input=body.encode("utf-8", "surrogatepass"), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "", "", True
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"), False


def _log_size(day):
    try:
        return os.path.getsize(day.log)
    except OSError:
        return 0


def _log_since(day, before):
    try:
        with open(day.log, "rb") as f:
            f.seek(before)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _last(tail):
    lines = [ln.strip() for ln in (tail or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "traceback"


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_DIGITS = re.compile(r"\d+")


def _key_line(tail):
    """The last traceback line with quoted strings and numbers blanked, so 'resume_1' at 12 and 'other_22' at 99 are one
    bug."""
    return _DIGITS.sub("N", _QUOTED.sub("Q", _last(tail)))


def signature(crash):
    """The dedupe key of a crash: its event and its normalised last traceback line, so one bug is one FIX."""
    event, tail = crash[0], crash[2]
    return hashlib.sha1(f"{event}\n{_key_line(tail)}".encode("utf-8", "replace")).hexdigest()[:12]


def run(seed, budget_s, upto=None):
    """Mutated payloads through the drill's hooks until the budget runs out (budget_s None: no bound, only with upto).
    upto stops after that many mutants: a replay. Returns {seed, budget_s, ran, elapsed, crashes}: crashes are
    (event, body, stderr tail, mutant index), one per distinct signature; a hook past its ceiling is a crash unless the
    run's own bound cut it short."""
    if budget_s is None and upto is None:
        raise ValueError("an unbounded run needs upto (a replay)")
    rng = random.Random(seed)
    crashes, seen, ran = [], set(), 0
    day = _day()  # setup first: the budget and elapsed are the fuzzing's, not the day's
    try:
        t0 = time.monotonic()
        deadline = None if budget_s is None else t0 + max(0.0, budget_s)
        ceiling = _ceiling()
        for k, (event, _kind, body) in enumerate(_mutants(rng)):
            if upto is not None and k >= upto:
                break
            timeout = ceiling if deadline is None else min(ceiling, deadline - time.monotonic())
            if timeout <= 0:
                break
            before = _log_size(day)
            rc, out, err, timed = _exec(day, event, body, timeout)
            ran += 1
            text = (out or "") + (err or "") + _log_since(day, before)  # a traceback the hook logged, not printed
            crash = None
            if drill.crash(event, rc, "", text):
                crash = (event, body, text[-TAIL:], k)
            elif timed and (deadline is None or time.monotonic() < deadline):
                crash = (event, body, "timed out", k)
            if crash and signature(crash) not in seen:
                seen.add(signature(crash))
                crashes.append(crash)
    finally:
        day.remove()
    return {"seed": seed, "budget_s": budget_s, "ran": ran, "elapsed": round(time.monotonic() - t0, 2),
            "crashes": crashes}


def brief_text(crash, seed):
    """The FIX's text: the signature first (the dedupe key), the event and last traceback line, the replay (the seed and
    the mutant's index: fm regenerates the payload, so no quoting and no NUL loss) and the stderr tail."""
    event, _body, tail, k = crash
    return (f"spar:{signature(crash)} {event}: {_last(tail)}\n\n"
            f"A hook crashed on mutant {k} of seed {seed}. Replay it in a sandboxed temp home:\n"
            f"  sampo spar --seed {seed} --replay {k}\n\n"
            f"stderr tail:\n{tail[-600:]}")


def capture(p, report):
    """[(brief, new)] per crash: eco.capture_once with the signature as its key, so a repeat (tonight's run, a second one)
    is not filed again. Past MAX_FILED new FIXes in this run a crash is (None, False): not filed now, a later run that
    finds it files it."""
    from sampo import eco
    out, filed = [], 0
    for crash in report["crashes"]:
        if filed >= MAX_FILED:
            out.append((None, False))
            continue
        brief, new = eco.capture_once(p, brief_text(crash, report["seed"]), "FIX", "spar",
                                      key=f"spar:{signature(crash)}")
        filed += bool(new)
        out.append((brief, new))
    return out


def for_json(rep):
    """The report as --json shows it: crash bodies clipped to JSON_BODY chars, so a 200 KB mutant is not printed whole."""
    def clip(body):
        body = body.encode("utf-8", "backslashreplace").decode("utf-8")  # a raw mutant's lone surrogate: escaped, not printed
        return body if len(body) <= JSON_BODY else f"{body[:JSON_BODY]}...[{len(body)} chars]"
    return dict(rep, crashes=[(ev, clip(body), tail, k) for ev, body, tail, k in rep["crashes"]])


def render(r):
    lines = [f"sampo spar seed {r['seed']}: {r['ran']} hook run(s) in {r['elapsed']} s ({r['priority']} priority), "
             f"{len(r['crashes'])} crash(es)"]
    for crash in r["crashes"]:
        lines.append(f"  ✗ {crash[0]} (mutant {crash[3]}): {signature(crash)} {_last(crash[2])}")
    if "replay" in r:
        lines.append(f"  mutant {r['replay']}: {'the crash recurs' if r['recurs'] else 'no crash'}")
    if "filed" in r:
        lines.append(f"  filed {r['filed']} FIX(es)")
    return "\n".join(lines)


def cmd_spar(args):
    """sampo spar [--seed N] [--budget S] [--capture] [--replay K] [--json]: the nightly fuzz. Idle priority (nice 19), since
    the night runs it beside the lanes; --capture files each crash as a FIX with its replay line; --replay K reruns
    mutants 0..K of --seed in a fresh sandboxed day and says whether mutant K still crashes."""
    from sampo import cli
    try:
        os.nice(19)
        pri = "idle"
    except OSError:
        pri = "normal"
    if args.replay is not None:
        if args.seed is None:
            raise SystemExit("sampo spar: --replay needs --seed (a FIX's brief names both)")
        rep = run(args.seed, None, upto=args.replay + 1)
        rep["replay"] = args.replay
        rep["recurs"] = any(crash[3] == args.replay for crash in rep["crashes"])
    else:
        seed = args.seed if args.seed is not None else secrets.randbelow(2 ** 31)
        rep = run(seed, args.budget)
    rep["priority"] = pri
    if args.capture:
        got = capture(cli.resolve(args), rep)
        rep["filed"] = sum(1 for _, new in got if new)
    cli.out(args, for_json(rep), render(rep))
