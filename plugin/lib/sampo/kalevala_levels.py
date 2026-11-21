"""Kalevala levels (T-5013, slice of T-3312): one generic level type, no per-depth code. Level k's units are whole
level k-1 setups, each with the count of graded samples behind it. select() picks the best setup by measured result,
or says NOT_ENOUGH_SIGNAL when the level below has too few graded samples to tell setups apart (the floor is an input:
rune's exam floor is 3 graded per capability). budget() is geometric, so the budgets of every level from 2 up to any
depth total under twice level 2's. cap() is the wall-clock cap of one level-k tick, and level k ticks once per
ROUNDS_PER_TICK level k-1 rounds (due()). Pure functions: no clock, no files, no wiring into kalevala.py."""
import dataclasses
import math

NOT_ENOUGH_SIGNAL = "not enough signal"
MAX_DEPTH = 16  # a bound on k: cap() grows as ROUNDS_PER_TICK ** k, so an unbounded k would be an unbounded integer
ROUNDS_PER_TICK = 3  # level k ticks once per this many level k-1 rounds
CAP_BASE = 3600  # seconds: the wall-clock cap of one level-1 round; level k's is ROUNDS_PER_TICK ** (k-1) times it


@dataclasses.dataclass(frozen=True)
class Unit:
    """One whole level k-1 setup and how many graded samples measure it."""
    name: str
    graded: int = 0

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name or self.name == NOT_ENOUGH_SIGNAL:
            raise ValueError(f"unit name must be a non-empty string other than {NOT_ENOUGH_SIGNAL!r}")
        if isinstance(self.graded, bool) or not isinstance(self.graded, int) or self.graded < 0:
            raise ValueError("graded must be a count of 0 or more")


@dataclasses.dataclass(frozen=True)
class Level:
    """Level k: the same type at every depth. Its units are the setups of level k-1."""
    k: int
    units: tuple = ()

    def __post_init__(self):
        _depth(self.k)
        names = [u.name for u in self.units]
        if len(set(names)) != len(names):
            raise ValueError("unit names in one level must differ")


def _depth(k, low=1):
    if isinstance(k, bool) or not isinstance(k, int) or not low <= k <= MAX_DEPTH:
        raise ValueError(f"level must be an integer from {low} to {MAX_DEPTH}, got {k!r}")
    return k


def _number(x):
    """x as a finite float, or None for a missing, non-numeric or non-finite result."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def select(level, results, floor):
    """The name of the unit with the best measured result, or NOT_ENOUGH_SIGNAL. results maps unit name to its result
    (higher is better); floor is the fewest graded samples a unit needs to count. A unit under the floor or without a
    finite result never wins; fewer than two counted units, or an exact tie for best, can't tell setups apart."""
    if isinstance(floor, bool) or not isinstance(floor, int) or floor < 1:
        raise ValueError("floor must be an integer of 1 or more")
    scored = []
    for u in level.units:
        r = _number(results.get(u.name))
        if u.graded >= floor and r is not None:
            scored.append((r, u.name))
    if len(scored) < 2:
        return NOT_ENOUGH_SIGNAL
    scored.sort(reverse=True)
    return NOT_ENOUGH_SIGNAL if scored[0][0] == scored[1][0] else scored[0][1]


def _frac(frac):
    f = _number(frac)
    if f is None or not 0 < f <= 0.5:  # a ratio over 1/2 lets the sum over depth pass twice level 2's
        raise ValueError("frac must be over 0 and at most 0.5")
    return f


def budget(k, base, frac):
    """The budget share of level k (k >= 2): base is level 2's, each level above gets frac of the one below."""
    _depth(k, 2)
    b = _number(base)
    if b is None or b < 0:
        raise ValueError("base must be a finite number of 0 or more")
    return b * _frac(frac) ** (k - 2)


def total(depth, base, frac):
    """The budgets of levels 2 through depth added up: under twice budget(2, base, frac) at any depth."""
    return sum(budget(k, base, frac) for k in range(2, _depth(depth, 2) + 1))


def cap(k):
    """The wall-clock cap in seconds of one level-k tick: it spans ROUNDS_PER_TICK level k-1 rounds."""
    return CAP_BASE * ROUNDS_PER_TICK ** (_depth(k) - 1)


def due(k, rounds_below):
    """True when level k ticks after rounds_below completed level k-1 rounds: every ROUNDS_PER_TICK of them
    (level 1 has nothing below it and ticks every round)."""
    _depth(k)
    if isinstance(rounds_below, bool) or not isinstance(rounds_below, int) or rounds_below < 0:
        raise ValueError("rounds_below must be a count of 0 or more")
    return rounds_below > 0 and (k == 1 or rounds_below % ROUNDS_PER_TICK == 0)
