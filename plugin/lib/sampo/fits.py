"""T-3499: situation probes: a Sampo feature whose moment is now, named with its numbers. On 2026-11-06 sampo usage
showed 112 of 171 verbs never run and 23 of 26 skills never loaded while tidy, release and the debug-test skill each
had their moment (seven stale scratch worktrees, 322 unreleased CHANGELOG entries, seven failed closes that day), and
the user asked why they sat unused. PROBES maps a feature (a verb or skill name, as sampo usage counts it) to
fn(p, briefs, now): a short reason with its numbers, or None. Each is a cheap state read, at most one git call.
selfcheck.feature_fit names the hits that sampo usage also counts unused; the night runs the READ_ONLY ones that fit."""
import os
import tempfile
import time

from sampo import core as c

TIDY_DAYS, DOCTOR_DAYS, SCRATCH_H, WINDOW_DAYS = 7, 7, 1, 30
READ_ONLY = ("doctor", "tidy", "usage", "repeats")  # verbs the night may run: none writes outside Sampo's state


def _days(ts, now):
    t = c.parse_ts(ts)
    return None if t is None else (now - t.timestamp()) / 86400


def _n(k, word):
    return f"{k} {word}{'' if k == 1 else 's'}"


def tidy(p, briefs, now):
    """Scratch worktrees git still lists whose folder is gone, or sits under the temp dir SCRATCH_H+ hours old (a
    verify or train run of now is live), and the days since the last sampo tidy --apply."""
    tmp, stale = tuple(os.path.realpath(d) + os.sep for d in ("/tmp", "/var/tmp", tempfile.gettempdir())), 0
    for block in c._git(p.root, "worktree", "list", "--porcelain", timeout=10).strip().split("\n\n")[1:]:
        path = next((ln[9:] for ln in block.splitlines() if ln.startswith("worktree ")), "")
        try:
            stale += os.path.realpath(path).startswith(tmp) and now - os.path.getmtime(path) >= SCRATCH_H * 3600
        except OSError:
            stale += 1  # git lists it, the folder is gone
    days = _days(c.read_meta(p).get("last_tidy"), now)
    if not stale and (days is None or days < TIDY_DAYS):  # never tidied is no age: a new project has nothing to tidy
        return None
    why = ([_n(stale, "stale scratch worktree")] if stale else []) \
        + ([f"last tidy {days:.0f} days ago"] if days is not None else [])
    return f"sampo tidy ({', '.join(why)})"


def release(p, briefs, now):
    """Sampo's own repo (plugin.json holds the version): doctor's release-age verdict, the Unreleased CHANGELOG
    entries and the days since the newest release heading, where the version last moved."""
    if not os.path.isfile(os.path.join(p.root, "plugin", ".claude-plugin", "plugin.json")):
        return None
    from sampo import doctor
    r = doctor.check_release_age(p.root)
    return f"sampo release ({r.detail.rsplit(':', 1)[0]})" if r.status == "WARN" else None


def debug_test(p, briefs, now):
    """Closes refused today because an affected test failed (ledger close_affected, a non-zero exit)."""
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    n = sum(bool((e.get("data") or {}).get("exit")) for e in c.ledger_events(p, ("close_affected",), since=today))
    return f"the debug-test skill ({_n(n, 'failed close')} today)" if n else None


def doctor(p, briefs, now):
    """Days since sampo doctor last ran here (meta doctor_at, which cmd_doctor writes), or since the project was set
    up when it never did."""
    meta = c.read_meta(p)
    days = _days(meta.get("doctor_at") or meta.get("created"), now)
    if days is None or days < DOCTOR_DAYS:
        return None
    return f"sampo doctor ({'last run' if meta.get('doctor_at') else 'never run here, set up'} {days:.0f} days ago)"


PROBES = {"tidy": tidy, "release": release, "debug-test": debug_test, "doctor": doctor}


def _hits(p, briefs, now, names):
    out = {}
    for name in names:
        try:
            hit = PROBES[name](p, briefs, now)
        except Exception:  # noqa: BLE001 — a probe never costs the next action
            hit = None
        if hit:
            out[name] = hit
    return out


def unused(p, names):
    """Of these feature names, the ones sampo usage counts unused over WINDOW_DAYS: no run, no load, no read."""
    from sampo import cost
    n = cost.counts(p, cost._since(WINDOW_DAYS))
    return [x for x in names if not (n["commands"][x] or n["skills"][x] or n["playbooks"][x])]


def hits(p, briefs, now):
    """The probes' reasons for the features that fit now and are unused, in PROBES order (the usage scan only once
    a probe hits)."""
    got = _hits(p, briefs, now, PROBES)
    return [got[x] for x in unused(p, list(got))] if got else []


def due(p, briefs, now):
    """The READ_ONLY verbs the night runs: one with a probe when its situation holds, a plain report every night."""
    return [v for v in READ_ONLY if v not in PROBES or _hits(p, briefs, now, [v])]
