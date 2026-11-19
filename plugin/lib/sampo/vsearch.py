"""T-3010: verifier-guided search, the policy alone. A small model's first try at a step is often wrong and its fourth
right; the step's own check is a cheap, exact judge. `search` takes the model's first candidate and up to N-1 more
drawn on demand, runs `check` on each, stops at the first green and otherwise keeps the one that fails fewest (the
earliest on a tie). It never calls a model or runs a command itself: the caller hands in `draw` and `check`, so the
policy is tested with scripted ones and smith.py owns the copies, the prompts and the journal.

Models advise, proof decides: a candidate is kept because its check passed, never because the model liked it."""
import collections
import math
import re

MAX_N = 8  # the most candidates one search checks, whatever --search says (time, tokens: one bound)
AUTO_MAX = 6  # --search auto on a quick check
DEFAULT_N = 4  # --search auto when the check's time is unknown or between the two bounds
QUICK_S, SLOW_S = 10.0, 60.0  # under QUICK_S a check is cheap (AUTO_MAX draws); over SLOW_S it is dear (2 draws)
SLOW_N = 2

Verdict = collections.namedtuple("Verdict", "outcome failed seconds")  # outcome: green | red | unusable
Result = collections.namedtuple("Result", "cand index green tried n verdict")  # cand None: nothing usable


def _secs(x):
    """A finite non-negative number of seconds, else None (a bool, text, NaN or a negative is no measurement)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
        return None
    return float(x)


def choose_n(seconds):
    """How many candidates a check that takes `seconds` allows: under 10 s up to AUTO_MAX, over 60 s two, between (and
    when the time is unknown) DEFAULT_N."""
    s = _secs(seconds)
    if s is None:
        return DEFAULT_N
    return AUTO_MAX if s < QUICK_S else SLOW_N if s > SLOW_S else DEFAULT_N


NUDGES = (  # one a draw after the first: another way in, so the draws differ even where the server ignores temperature
    "Take a different approach to this edit than your first thought: change a different line or use a different "
    "construct, and keep it as small as you can.",
    "Reread the failing check's expectation before you answer, then make the smallest edit that meets it exactly.",
    "Think about which assumption in the code is wrong, not only where the symptom shows, and edit that.",
    "Answer with the most conventional fix a maintainer of this code would write, in the fewest lines.")


def temperature(i):
    """The sampling temperature of draw i (1-based; the model's own first reply is draw 0, at its default): 0.5, 0.7, 0.9,
    then 1.0, so each extra draw is warmer than the one before."""
    return round(min(1.0, 0.3 + 0.2 * max(int(i), 1)), 2)


def nudge(i):
    """The one-sentence note draw i carries (draw 1 the first of NUDGES, round and round)."""
    return NUDGES[(max(int(i), 1) - 1) % len(NUDGES)]


def parse(value):
    """--search's value: a count 1..MAX_N or 'auto' (case and spaces ignored); ValueError, naming the option, otherwise."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text == "auto":
        return "auto"
    if re.fullmatch(r"[0-9]{1,3}", text) and 1 <= int(text) <= MAX_N:
        return int(text)
    raise ValueError(f"--search is a count from 1 to {MAX_N} or auto, not {value!r}")


def search(n, first, draw, check, journal=None, auto=False, hint=None):
    """Result(cand, index, green, tried, n, verdict). `first` is candidate 0, drawn by the caller (it is the model's own
    reply, so N=1 is today's try, checked and journaled); draw(i) returns candidate i or None (unusable); check(cand)
    returns a Verdict. auto sizes n with choose_n(hint) before the first check, else from the first measured check's
    seconds (DEFAULT_N until one is). journal(**fields) is told each candidate and the pick. cand is None when nothing
    was usable, the green one when any is green, else the usable one that failed fewest (the first on a tie)."""
    say = journal or (lambda **_: None)
    sized = auto and _secs(hint) is not None
    limit = choose_n(hint) if sized else DEFAULT_N if auto else min(max(int(n), 1), MAX_N)
    best = None  # (failed, index, cand, verdict)
    tried, i = 0, 0
    while i < min(limit, MAX_N):
        cand = first if i == 0 else draw(i)
        tried += 1
        v = check(cand) if cand is not None else Verdict("unusable", 0, None)
        if v.outcome not in ("green", "red"):
            v = Verdict("unusable", v.failed, v.seconds)
        say(event="candidate", cand=i, outcome=v.outcome, failed=v.failed, seconds=v.seconds)
        if auto and not sized and _secs(v.seconds) is not None and v.outcome != "unusable":
            sized, limit = True, choose_n(v.seconds)
        if v.outcome == "green":
            best = (0, i, cand, v)
            break
        if v.outcome == "red" and (best is None or v.failed < best[0]):
            best = (v.failed, i, cand, v)
        i += 1
    green = bool(best) and best[3].outcome == "green"
    out = Result(best[2] if best else None, best[1] if best else None, green, tried, limit,
                 best[3] if best else Verdict("unusable", 0, None))
    say(event="pick", cand=out.index, outcome=out.verdict.outcome, failed=out.verdict.failed, green=green, tried=tried,
        n=limit, auto=bool(auto))
    return out
