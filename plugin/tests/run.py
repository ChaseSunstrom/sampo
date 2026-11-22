#!/usr/bin/env python3
"""The test suite across the cores (T-0133): the tests `unittest discover` finds, one test class per process (each
test already gets its own FOREMAN_HOME and repo), so the gate waits for the slowest class instead of the sum of all.
Exit 1 on any failure, with each failing class's report.

  python3 plugin/tests/run.py [-j N] [-k PATTERN] [-p GLOB] [--load N] [NAME ...]
"""
import argparse
import ast
import atexit
import builtins
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings

try:
    import fcntl
except ImportError:  # Windows: no flock, so no slot pool; classes run as before
    fcntl = None
try:
    import resource
except ImportError:  # Windows: the run's own CPU goes uncounted
    resource = None

HERE = os.path.dirname(os.path.abspath(__file__))


def child_env(tmp):
    """A class's environment: its own TMPDIR, and local models hidden as on CI unless the caller set the switch
    (T-3229: the moons tests leaned on this box's Ollama embedders, passed here and failed on every CI run)."""
    env = {k: v for k, v in os.environ.items() if not (k.startswith(("FOREMAN_", "SAMPO_")) and k.split("_", 1)[1] in OPERATOR_SWITCHES)  # T-3625
           and not k.startswith(override_prefixes())}  # T-4289: a test sets its own FM_STALL_ and kin, never its caller
    return dict(env, TMPDIR=tmp, FOREMAN_NO_LOCAL_MODELS=os.environ.get("FOREMAN_NO_LOCAL_MODELS", "1"))


_OVERRIDES = None


def override_prefixes():
    """T-4289 review: core.TEST_OVERRIDES, the one list, read from the lib's source (run.py imports no sampo module, so
    the file is read, once); () when the file isn't beside this runner (a copy of run.py alone)."""
    global _OVERRIDES
    if _OVERRIDES is None:
        _OVERRIDES = ()
        with contextlib.suppress(OSError, ValueError, SyntaxError):
            with open(os.path.join(HERE, "..", "lib", "sampo", "core.py"), encoding="utf-8") as f:
                m = re.search(r"^TEST_OVERRIDES = (\(.*?\))$", f.read(), re.M | re.S)
            if m:
                _OVERRIDES = tuple(ast.literal_eval(m.group(1)))
    return _OVERRIDES

# T-3625: switches an operator sets for one run (either prefix); a test that needs one sets it itself
OPERATOR_SWITCHES = {"LOAD_GATE", "LOAD_WAIT", "FULL_GATE", "SENTINEL", "SENTINEL_RUN", "SENTINEL_TREE", "NO_BACKGROUND",
                     "SLOT", "SLOTS", "SLOT_PRIORITY", "NO_SUITE_SLOT", "GATE_HELD", "GATE_WAIT", "SLOW_GATE", "INHIBITED",
                     "DRIVE_TASK", "QUIET", "NO_NET", "NO_LINK", "CONFINE", "SMOKE_RUNNER", "UI_CACHE_S", "NO_TEST_CACHE",
                     "FAST",  # T-3571: SAMPO_FAST=0 sends every call past sampo-fast; test_fast_equiv needs it on
                     "CPU_BASE", "CPU_GATE", "CPU_FLOOR_S",  # T-3757: the train's CPU baseline is never a test's
                     "SHAPE_RUNS"}  # T-4947: how many lanes' repo-shape stages run at once


SLOT_ENV = "SAMPO_SLOT"  # T-3252: set in a class's children, so a run.py inside a slot never waits on its parent's pool
OPERATOR_SWITCHES.add("LANE_PREVIEW_BUDGET_S")  # T-3268: the lane list preview's time budget; a test sets its own
OPERATOR_SWITCHES.add("LAND_NICE")  # T-5527: the nice level a land or train lowers itself to; a test sets its own
OPERATOR_SWITCHES.add("NO_JOIN")  # T-4267: a run's own switch, never a test's
OPERATOR_SWITCHES.add("SPAWN_TABLE")  # T-5835: =off turns the spawn table's fill and lookup refusal off; a test sets its own
OPERATOR_SWITCHES.add("LANE_PILOT")  # T-5708: =off stops the detached lane pilots; a test of the pilot sets its own
OPERATOR_SWITCHES.add("NO_REEXEC")  # T-5735: stops a long-running loop's re-exec onto new code; a test of the re-exec sets its own
OPERATOR_SWITCHES.add("PROCESS_TIER")  # T-5713: =off gives today's S, M and L tiers; a test that needs it sets it itself
OPERATOR_SWITCHES.add("CARD_ORIGIN")  # T-5613: a bench arm names its calls "bench"; never a test's origin, a test sets its own
OPERATOR_SWITCHES.add("TRAIN_PROBES")  # T-3770: a runner's probe switch is never a test's
OPERATOR_SWITCHES.add("REVIEW_DEPTH")  # T-5841: =full gives every review all five lenses; a test that needs it sets it itself
OPERATOR_SWITCHES.add("TOWER_GAP")  # T-3504: the tower-update push gap; a test that needs it sets it itself
OPERATOR_SWITCHES.add("TEST_RUN_ID")  # T-4268: the check's id for its gate's record; a nested run in a class never gets it
OPERATOR_SWITCHES.add("CACHE_FULL")  # T-5482: a whole run skips its cached classes (the train and the behind verify set it); never a test's
OPERATOR_SWITCHES.add("HOLD_JOBS")  # T-5465: how many lane holds run at once; the operator's knob, a test sets its own
OPERATOR_SWITCHES.add("SHAPE_BATCH_WALL_S")  # T-5380: a shape batch's wall in seconds; the operator's, a test passes its own
OPERATOR_SWITCHES.add("BENCH_MIN_FREE_GB")  # T-3747: the operator's disk budget for bench builds; the low-disk test sets its own
OPERATOR_SWITCHES.add("NO_COVERED")  # T-4288: the "Already covered?" note's switch; helpers set the tests' default, Covered its own
# T-4239: a land's ship pin, caps and line stamp are the land's (its gate runs these tests inside it), never a test's
OPERATOR_SWITCHES.update({"SHIP_AT", "SHIP_TIMEOUT_S", "LAND_SHIP_WAIT_S", "LAND_STAMP", "LAND_STANDING_SHIP"})  # T-5938: the last is the land's marker on its standing ship
OPERATOR_SWITCHES.add("BIG_HOLD_S")  # T-4223: an operator's turn budget for a big task; a hold test sets its own
OPERATOR_SWITCHES.update({"WAIT_LIMIT_S", "WAIT_SETTLE_S", "WAIT_TRAIN_POLL_S"})  # T-4525, T-4692: the wait timer's limits; its tests set their own
OPERATOR_SWITCHES.add("LANE_CHECKS")  # T-4491: how many lane checks run at once; an accept test sets its own
OPERATOR_SWITCHES.add("NO_PROMPT_MAC")  # T-4844: leaves prompt events unmarked; the hook tests set it themselves, a signing test clears it
OPERATOR_SWITCHES.add("KEEP_ORPHAN")  # T-4538: a detached run that outlives its starter; a test that needs it sets it itself
OPERATOR_SWITCHES.add("SLOT_RESERVE")  # T-4650: the reserved slots' off switch is the operator's, never a test's
OPERATOR_SWITCHES.add("SLOT_TRACE")  # T-4685: the slot trace is an operator's, never a test's
OPERATOR_SWITCHES.add("SLOT_FAIR")  # T-4688: the fair share's off switch is the operator's, never a test's
OPERATOR_SWITCHES.add("NO_CPU_IDLE")  # T-4988: helpers set it so no test idles the session's real CPU scope
OPERATOR_SWITCHES.add("SLOT_LOW")  # T-4570: the post-merge verify's runs yield slots to waiting builders; never a test's
OPERATOR_SWITCHES.add("NO_LEFTOVER_SCAN")  # T-4720: the test base sets it itself; a caller's never reaches a test of the scan
OPERATOR_SWITCHES.add("CARGO_CAP_BYTES")  # T-4717: the operator's cap on the shared cargo cache; a test sets its own
OPERATOR_SWITCHES.add("CARGO_FREE_FLOOR_BYTES")  # T-5874: the free-space floor that shrinks that cap; a test sets its own
OPERATOR_SWITCHES.add("CARGO_SHARED")  # T-4717: the operator's off switch for the shared cargo target; a test sets its own
OPERATOR_SWITCHES.add("KNOWN_RED_MIN")  # T-4269: how long a red class's record is believed; a nested run's is its own
OPERATOR_SWITCHES.add("EVIDENCE_TREE")  # T-3536: the tree an evidence run started on, for the command it runs; a test never inherits it
OPERATOR_SWITCHES.add("SESSION_SLOTS_DIR")  # T-5386: where an exam's endpoint session slots are locked; a slot test sets its own
CGROUP_ROOT = "/sys/fs/cgroup"
LIVE_CLASSES = set()  # T-4538: the class processes running now (each leads its own session, so its pid is its group)
PARENT_POLL_S = 1.0  # T-4538: how often a run checks that the process which started it is still alive
ENDED = []  # T-4399: the signal (SIGTERM, SIGINT) that ended this run from outside, set by its handler; the waits read it
ENDED_TEXT = "ended from outside"  # what a class killed by such an end, and the summary, say: 'not run', never 'failed'
ENDPOLL_S = 0.5  # T-4399: how often a class's wait reads whether the run was ended (a few wake-ups a second, not more)


def _env(name):
    """T-3442: SAMPO_X, or FOREMAN_X once importing sampo renamed it (core.mirror_env), as in every test process."""
    return os.environ.get(name) or os.environ.get("FOREMAN_" + name[len("SAMPO_"):])


def _cgroup_dir():
    """This process's cgroup v2 folder, or None."""
    try:
        with open("/proc/self/cgroup") as f:
            rel = next((ln.split("::", 1)[1].strip() for ln in f if ln.startswith("0::")), None)
    except OSError:
        return None
    return os.path.join(CGROUP_ROOT, rel.lstrip("/")) if rel else None


def usable_cpus(folder=None):
    """T-4531: sampo core.usable_cpus for this runner, a copy because run.py must count before sampo is importable (a
    copy of run.py alone): the smallest of os.cpu_count(), the scheduler affinity and each cgroup v2 folder's cpu.max
    from `folder` up to CGROUP_ROOT (quota over period, rounded up, "max" none), at least 1. test_cpu_cap_count keeps
    the two equal."""
    counts = [os.cpu_count() or 4]
    try:
        counts.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    top = os.path.realpath(CGROUP_ROOT)
    d = folder
    while d and _under(os.path.realpath(d), top):
        try:
            with open(os.path.join(d, "cpu.max")) as f:
                quota, period = f.read().split()[:2]
            if quota != "max":
                counts.append(max(1, math.ceil(int(quota) / int(period))))
        except (OSError, ValueError, ZeroDivisionError):
            pass
        if os.path.realpath(d) == top:
            break
        d = os.path.dirname(d)
    return max(1, min(counts))


def _under(path, top):
    return path == top or path.startswith(top.rstrip(os.sep) + os.sep)


def slot_count():
    """T-3252: the CPUs every run.py on the machine shares: SAMPO_SLOTS, else the CPU count cut to the tightest cgroup
    quota above this process (sampo cpu cap 50 on 16 CPUs gives 8)."""
    try:
        return max(0, int(_env("SAMPO_SLOTS")))
    except (TypeError, ValueError):
        pass
    n, d = os.cpu_count() or 4, _cgroup_dir()
    while d and os.path.realpath(d).startswith(os.path.realpath(CGROUP_ROOT)):
        try:
            with open(os.path.join(d, "cpu.max")) as f:
                quota, period = f.read().split()[:2]
            if quota != "max":
                n = min(n, max(1, math.ceil(int(quota) / int(period))))
        except (OSError, ValueError):
            pass
        if os.path.realpath(d) == os.path.realpath(CGROUP_ROOT):
            break
        d = os.path.dirname(d)
    return n


@contextlib.contextmanager
def cpu_slot(wait=3600.0, poll=0.05, small=False, low=False, narrow=False):
    """T-3252: one of the machine's CPU slots, shared by every run.py on it (mine, each builder's, the ship's, the
    post-merge verify), held while the block runs: its number, or None when nested, off (SAMPO_SLOTS=0) or the wait ran
    out (the class then runs anyway: a slow run beats a hung one). A slot is a lock file, so the kernel frees it when
    its holder dies and a crashed runner leaks nothing. T-3614: while a SAMPO_SLOT_PRIORITY=1 run waits, other runs
    leave freed slots to it (the merge train's gate got 1/N of them beside ~10 testing builders) for defer_for(wait),
    then compete again, so a lane's run under a deadline isn't left NOT_STARTED by a long gate. T-4247: a small run (a
    selection of at most EXPRESS_CLASSES classes) first tries an express slot, without deferring to a priority waiter,
    so one or two classes start beside a gate; when those are taken it competes for the pool as any run does. T-4536: a
    small run waits at most SMALL_WAIT for either, then runs with none (a 14 s module's 13 per-test jobs, two on express,
    queued 786 s behind full suites); a run that waits SAY_WAIT or more says so once, with the seconds waited so far.
    T-4570: `low` (the post-merge verify's runs, SAMPO_SLOT_LOW=1) leaves freed slots to a waiting ordinary run too: a
    waiting class holds a wait- marker (one per process), and a low class does not take a slot while one is there, for
    LOW_MAX seconds from the process's first yield (_builder_waiting), then competes as before. T-5050: a gate (a
    priority run that is not small: its marker ends GATE_SUFFIX) is left freed slots for min(half the run's wait, GATE_MAX),
    not defer_for(wait). Trade-off: a class that is neither small nor narrow and already holds a slot defers to a gate that
    long, so under a deadline of D s it starts after D/2 at the latest (never NOT_STARTED by a gate), and a gate waiting past
    GATE_MAX is shared again; a small or narrow run still takes the reserve's slots beside it at once."""
    n = slot_count()
    folder = _slot_folder() if n and not _env(SLOT_ENV) and fcntl else None
    if not folder:  # nested, off, no flock (Windows) or a folder that isn't safely ours: run without one
        yield None
        return
    fast, ordinary = lanes(n)
    now, order, prio, mark = time.monotonic(), random.sample(ordinary, len(ordinary)), _env(PRIO_ENV) == "1", None
    end, defer, got, said = now + (min(wait, SMALL_WAIT) if small else wait), now + defer_for(wait), None, False
    gate_defer = now + min(wait / 2, GATE_MAX)  # T-5050: a gate is deferred to far longer than a plain marker, still not to the end
    told = False  # T-3421: this class wrote the process's one 'waiting' line, so it writes the 'got' line
    low, marked = low and not prio, False
    fair = fair_on()
    if fair:
        tidy_slots(folder, n)
    # T-4685: a narrow run takes the reserve's slots but the last after the pool's own, and without deferring to a priority
    # waiter there (a gate draws on the ordinary slots alone, so it never wants the reserve)
    extra = narrow_lane(n) if narrow and not small and not prio and not low else ()
    try:
        while True:
            if small and (got := _take(folder, fast)):
                break
            # T-4789: why this pass leaves a free slot alone (the closing line counts it), and one read of the folder for it
            why, snap = None, {}
            if not prio:
                # T-4789: a run holding none yet defers only to a priority run that holds none either (its first slot goes
                # first, T-3614); a gate holding slots leaves the others their first, else its marker held a stream of short
                # runs off and the gate unmarked at every first slot it left: a race it lost. Held is this process's own
                # count (with a grace after its last), not the slot files, which read a builder between two jobs as none.
                # Fair off: the old rule, a priority waiter is deferred to whatever this run holds, for DEFER_MAX at most
                # across the process (every call restarted its own, so a marked waiter holding none held a class 300 s each)
                # A priority episode is one waiter's stay: a pass that sees no priority waiter ends it, so the next waiter (a
                # ship's gate after a small priority run) gets its own bounded deferral
                # T-5050: a gate's marker (-g: a priority run that is not small) is deferred to for as long as it waits, not
                # DEFER_MAX (a gate waiting 78 min drew 1/N of the freed slots after its first 5); it holds a marker only
                # while it waits, and small and narrow runs still take their reserve's slots first, above and below
                now_m, prio_w = time.monotonic(), _prio_waiting(folder)
                gate_w = prio_w and _prio_waiting(folder, suffix=GATE_SUFFIX)
                if not prio_w:
                    _DEFER["first"] = None
                bounded = now_m < defer and (_DEFER["first"] is None or now_m < _DEFER["first"] + DEFER_MAX)
                if prio_w and ((gate_w and now_m < gate_defer) or bounded) and (not fair or _holding() or _prio_first(folder, n, order, snap)):
                    why = LEFT_PRIO
                    if _DEFER["first"] is None and not gate_w:  # a gate's wait spends no plain marker's bound
                        _DEFER["first"] = now_m
                elif low and _builder_waiting(folder, order if fair else None, n, snap):
                    why = LEFT_LOW
            behind = bool(why)
            # T-4688: a run holding its share leaves a freed slot to a waiting run that holds less (a gate held all six for
            # 10+ minutes: each job that ended took the next at once, so a second run never found one free)
            share = fair and not behind and _yield_open() and _over_share(folder, n, order, prio, snap)
            if share:
                why = LEFT_SHARE
            if share:
                _yield_begin()  # one window for the process (FAIR_MAX), then a cooldown: a stuck waiter costs it once
            # T-4789: a prio run that refuses a free slot keeps its marker: the waiter it leaves it to holds none, and a run
            # holding none does not defer to a marker whose run holds a slot (the unmark let the other runs win 15 threads to
            # 3 for the rest)
            if not behind and not share:
                if got := _take(folder, order):
                    break
            if extra and (got := _take(folder, extra)):
                break
            if not said and time.monotonic() - now >= SAY_WAIT:
                said = True
                with _SAY_LOCK:  # T-4536: one line per run.py, not one per class waiting in its pool
                    if not _SAID:
                        _SAID.append(True)
                        told = True
                        sys.stderr.write(f"run.py: waiting for a CPU slot, {time.monotonic() - now:.0f} s\n")
                        sys.stderr.flush()
            if _env(TRACE_ENV) == "1":  # T-4685: what this refusal read
                _trace(folder, n, small, time.monotonic() - now, narrow)
            if ENDED or time.monotonic() >= end:  # T-4399: a run ended from outside takes no slot any more
                break
            if prio and not mark:
                mark = _mark(folder, suffix="" if small else GATE_SUFFIX)  # T-5050: a gate's is deferred to all its wait
            elif not prio and not low and not marked:  # T-4570: an ordinary run waiting is what a low class yields to
                marked = _wait_enter(folder, small and fair)  # the -s marker is T-4688's: fair off, the old rule
            began = time.monotonic()
            time.sleep(poll * random.uniform(1, 5))  # spread the retries of many waiters
            if why and _free_slot(folder, n, order):  # T-4789: a slot is still free after the pass: the count is a lower bound
                _left_add(why, began, time.monotonic())
    finally:
        if marked:
            _wait_leave()
        if mark:
            _unmark(mark)
    if not got:
        yield None
        return
    if told:  # T-3421: the class that said it was waiting says the wait ended: a caller's hang guard saw silence before
        sys.stderr.write(f"run.py: got a CPU slot after {time.monotonic() - now:.0f} s\n")
        sys.stderr.flush()
    counted = got[0] in ordinary  # T-4789: a reserve or express slot is not the pool's: it does not make a run a holder
    if counted:
        _held_add(1)
    try:
        yield got[0]
    finally:
        os.close(got[1])  # closing it frees the slot
        if counted:
            _held_add(-1)


EXPRESS = ("x0", "x1")  # T-4247: the express slots' lock files, beside the pool's "0".."N-1" in the same folder
EXPRESS_CLASSES = 2  # T-4247: a run of at most this many classes may take an express slot
DEFER_MAX = 300.0  # T-4247: a non-priority run leaves freed slots to a waiting priority run at most this long
GATE_SUFFIX = "-g"  # T-5050: a waiting gate's prio- marker ends so (not a small run's): DEFER_MAX does not bound it
GATE_MAX = 3600.0  # T-5050: what bounds a deferral to a gate instead: half the run's own wait, at most this (far above DEFER_MAX)
_DEFER = {"first": None}  # T-4789: when this process first deferred to the priority waiter now there: its DEFER_MAX runs from
# there, whichever class asks, and a pass that sees no priority waiter clears it (the next waiter gets its own)
SMALL_WAIT = 20.0  # T-4536: a small run waits at most this long for a slot (express or pool), then runs with none
SAY_WAIT = 5.0  # T-4536: a run that has waited this long writes one 'waiting for a CPU slot, N s' line to stderr
_SAID, _SAY_LOCK = [], threading.Lock()  # T-4536: the line is said once per run.py process, whichever class waited first
# T-4789: the reasons a run leaves a free slot alone (the closing line counts the wall seconds of each) and their spans
LEFT_SHARE, LEFT_PRIO, LEFT_LOW = "fair share", "a priority run", "the post-merge verify's yield"
LEFT_KEEP = 2048  # spans kept per reason before they are merged into their union (a long run polls tens of thousands of times)
_LEFT, _LEFT_LOCK = {}, threading.Lock()


LEFT_GAP = 0.1  # T-4789: spans of one reason this close (s) are one span: back-to-back passes differ by a file scan
_HELD, _HELD_LOCK = {"n": 0, "until": 0.0}, threading.Lock()  # T-4789: slots this process holds now, and when its last ended
HOLD_GRACE = 1.0  # T-4789: a process whose last slot just ended still counts as holding one this long (s)


def _held_add(delta):
    """T-4789: this process took (+1) or let go (-1) of a slot; the last one let go starts the grace."""
    with _HELD_LOCK:
        _HELD["n"] += delta
        if delta < 0:
            _HELD["until"] = time.monotonic() + HOLD_GRACE


def _holding():
    """T-4789: whether this process holds a slot, or let its last go less than HOLD_GRACE s ago: a builder whose job just
    ended is between two jobs, not a run holding none (the slot files read it holding none for an instant at every
    turnover, and it raced the gate for the freed slot as a first slot, T-3614)."""
    with _HELD_LOCK:
        return _HELD["n"] > 0 or time.monotonic() < _HELD["until"]


def _left_add(why, start, end):
    """T-4789: a pass of cpu_slot left a free slot alone for `why` from start to end (time.monotonic()). A span within
    LEFT_GAP s of the last one is one span with it (back-to-back passes), so a long wait keeps a few spans, not tens of
    thousands to sort; past LEFT_KEEP they are merged into their union."""
    with _LEFT_LOCK:
        spans = _LEFT.setdefault(why, [])
        if spans and max(start - spans[-1][1], spans[-1][0] - end) < LEFT_GAP:
            spans[-1] = (min(start, spans[-1][0]), max(end, spans[-1][1]))
            return
        spans.append((start, end))
        if len(spans) > LEFT_KEEP:
            spans[:] = merged_spans(spans)


def left_now():
    """T-4789: a copy of the spans kept so far, by reason."""
    with _LEFT_LOCK:
        return {why: list(spans) for why, spans in _LEFT.items()}


def _holders(folder, n, snap=None):
    """T-4789: {slot: pid} of the held slots; `snap` (one dict per pass of cpu_slot) keeps the one read for the others."""
    if snap is None:
        return slot_holders(folder, n)
    if "holders" not in snap:
        snap["holders"] = slot_holders(folder, n)
    return snap["holders"]


def _waiters(folder, snap=None):
    """T-4789: _waiter_pids, read once per `snap`."""
    if snap is None:
        return _waiter_pids(folder)
    if "waiters" not in snap:
        snap["waiters"] = _waiter_pids(folder)
    return snap["waiters"]


def _prio_first(folder, n, ordinary, snap=None):
    """T-4789: whether a waiting priority run holds none of the `ordinary` slots yet: its first slot goes before any other
    run's (T-3614), and once it holds one it owes the others theirs."""
    held = {pid for slot, pid in _holders(folder, n, snap).items() if slot in ordinary}
    return any(kind == "prio" and pid not in held for pid, kind in _waiters(folder, snap).items())


def _free_slot(folder, n, ordinary):
    """T-4789: whether one of the `ordinary` slots is free now."""
    held = slot_holders(folder, n)
    return any(s not in held for s in ordinary)


def merged_spans(spans):
    """T-4789: the union of (start, end) spans as sorted, disjoint spans (what wall_wait adds up)."""
    out = []
    for a, b in sorted(s for s in spans if s[1] > s[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def left_note(left, run_s=0.0):
    """T-4789: the summary's '; left free slots to others: fair share A s, a priority run B s, ... of R s wall' for each
    reason that kept a free slot from this run over 1 s of its wall clock (the union of its spans, as waited_note's
    wall), else ''. Fair share and the verify's yield are the runs' own waits for a slot that was free."""
    parts = [(why, wall_wait(spans)) for why, spans in left.items()]
    parts = [f"{why} {w:.0f} s" for why, w in sorted(parts, key=lambda p: -p[1]) if w > 1]
    if not parts:
        return ""
    wall = max([run_s, *(wall_wait(s) for s in left.values())])
    return f"; left free slots to others: {', '.join(parts)} of {wall:.0f} s wall"


def slot_notes(waits, wait_spans, left, run_s):
    """T-4789: the closing line's slot notes: what the run waited for a slot (waited_note), then what it left free to others."""
    return waited_note(waits, wait_spans, run_s) + left_note(left, run_s)


def defer_for(wait):
    """T-4247: how long a non-priority run leaves freed slots to a waiting priority run: half its wait, at most DEFER_MAX
    (it was half the wait, up to 1800 s, so a lane's run waited out a whole gate)."""
    return min(wait / 2, DEFER_MAX)


RESERVE_ENV = "SAMPO_SLOT_RESERVE"  # T-4650: =0 puts back the two extra express files and the two-class size bound
RESERVE_SLOTS = 2  # T-4650: pool slots only a small run takes, of a pool of RESERVE_POOL * RESERVE_SLOTS or more
RESERVE_POOL = 4  # ... a pool of n gets min(RESERVE_SLOTS, n // RESERVE_POOL) of them; under 4 slots none (express files)
# T-4650: a run is small, beyond EXPRESS_CLASSES classes, when it has at most SMALL_CLASSES classes and they are expected
# to take SMALL_WORK_S s together (their last times, else half a second a test). Measured 2026-11-14: builders' modules of
# 3 to 10 classes and 13 to 30 tests (under 1 s of work) waited 285 to 411 s behind gates; a module of 300 tests is 150 s.
SMALL_CLASSES, SMALL_WORK_S = 8, 15.0


def reserved(n):
    """T-4650: how many of the pool's n slots (the highest numbers) only a small run may take: so a gate that holds every
    other slot leaves them, and the processes at once stay within the pool. 0: the switch is off, or the pool is under
    RESERVE_POOL slots (a tiny pool keeps T-4247's two extra express files)."""
    return 0 if _env(RESERVE_ENV) == "0" else min(RESERVE_SLOTS, n // RESERVE_POOL)


def lanes(n):
    """T-4650: (the slot names a small run tries first, the names every run draws from) for a pool of n slots."""
    r = reserved(n)
    return (tuple(range(n - r, n)), range(n - r)) if r else (EXPRESS, range(n))


# T-4685: a narrow run (a handful of named modules) may take the reserve's slots but its last, once the pool's own are
# held. Measured 2026-11-14: a 3-module run waited 176 s of its wall behind ONE other run.py that held all 6 ordinary
# slots while both reserved slots sat idle; the pool is the cap, so the reserve's idle slots are inside Sampo's share.
NARROW_MODULES = 6


def narrow_lane(n):
    """T-4685: the reserved slots a narrow run may take after the ordinary ones: all but the highest, which stays a small
    run's alone. A pool with fewer than two reserved slots has none (a lone reserved slot stays small-only)."""
    r = reserved(n)
    return tuple(range(n - r, n - 1)) if r > 1 else ()


def is_narrow(names, k=(), p=()):
    """T-4685: whether a run is narrow: modules named (or -k), at most NARROW_MODULES of them, no -p glob. A whole-suite
    run (no names, -k or -p) and a run beyond that many modules are not: they queue for the pool as before."""
    return bool(names or k) and not p and len(names) <= NARROW_MODULES


def small_selection(classes, jobs=(), times=None, parts=None):
    """T-4247: whether a run of these test classes is small enough for a reserved slot: at most EXPRESS_CLASSES distinct
    classes, not tests. T-4650: or, with the reserve on, at most SMALL_CLASSES that take SMALL_WORK_S s over their jobs."""
    classes = set(classes)
    if len(classes) <= EXPRESS_CLASSES:
        return True
    if not reserved(slot_count()) or len(classes) > SMALL_CLASSES:
        return False
    return sum(job_seconds(j, times or {}, parts or {}) for j in jobs) <= SMALL_WORK_S


def _take(folder, names):
    """A free slot among names (the pool's numbers, or EXPRESS): (its name, its locked fd), or None."""
    for i in names:
        try:
            fd = os.open(os.path.join(folder, str(i)), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        try:  # T-4688: a file the pool's tidy removed after this open is a lock on nothing: another run makes a new one
            if os.stat(os.path.join(folder, str(i))).st_ino != os.fstat(fd).st_ino:
                raise OSError
        except OSError:
            os.close(fd)
            continue
        with contextlib.suppress(OSError):  # T-4708: the holder's pid, for the trace where /proc/locks is not there (macOS)
            os.ftruncate(fd, 0)
            os.pwrite(fd, str(os.getpid()).encode(), 0)
        return i, fd
    return None


FAIR_ENV = "SAMPO_SLOT_FAIR"  # T-4688: =0 puts back the old rule (any free slot is taken) and leaves the stale slot files
FAIR_MAX = 120.0  # T-4688: a process refuses a slot for another run's share at most this long in all, from its first refusal
FAIR_COOL = 300.0  # T-4688: after that window it does not refuse again for this long (one window, as _LOW's, not one per job)
FAIR_STUCK = 15.0  # T-4688: a waiter whose slots did not change in this long of refusals for it is stuck, not waited for
TIDY_AGE = 5.0  # T-4688: tidy_slots leaves a slot file touched more recently than this (a holder's take writes its pid)
TEMP_AGE = 60.0  # T-4688: a marker's temp file nobody holds is an orphan after this long
_TIDIED = set()  # T-4688: the (folder, pool size) whose slot files beyond the pool this process has already removed
_YIELD, _YIELD_LOCK = {"end": None, "cool": None, "seen": {}, "stuck": {}}, threading.Lock()  # window, cooldown, stuck clocks


def _yield_open():
    """T-4688: whether this process may still refuse a slot for another run's share: not in the cooldown that follows its
    window (FAIR_MAX from its first refusal, one window for every class of the run, not one per job)."""
    with _YIELD_LOCK:
        now = time.monotonic()
        if _YIELD["cool"] is not None:
            if now < _YIELD["cool"]:
                return False
            _YIELD["end"] = _YIELD["cool"] = None
            _YIELD["seen"].clear()
        if _YIELD["end"] is not None and now >= _YIELD["end"]:
            _YIELD["cool"] = _YIELD["end"] + FAIR_COOL  # from the window's end, not from this first call after it
            return False
        return True


def _yield_begin():
    """T-4688: this process refused a slot for another run's share: its window starts at the first such refusal."""
    with _YIELD_LOCK:
        if _YIELD["end"] is None:
            _YIELD["end"] = time.monotonic() + FAIR_MAX


def fair_on():
    """T-4688: whether runs share the ordinary slots fairly (SAMPO_SLOT_FAIR=0 turns it off)."""
    return _env(FAIR_ENV) != "0"


def fair_cap(ordinary, runs):
    """T-4688: how many of the `ordinary` slots one of `runs` runs may hold while another waits: its equal share, rounded
    up, and never under one (every run keeps one). Runs under 1 count as 1."""
    return max(1, -(-ordinary // max(1, runs)))


def _unmark(mark):
    """T-3614: take a waiting marker back, (fd, path) as _mark made it."""
    with contextlib.suppress(OSError):
        os.unlink(mark[1])
    os.close(mark[0])


def _waiter_pids(folder):
    """T-4688: {pid: its kind} of the runs waiting for a slot now: the live wait- and prio- markers (a marker nobody holds
    is a dead waiter's and is removed), the pid being the one in the marker's name. T-4789: the kind is "prio" (a priority
    run), "small" (a wait- marker ending -s: a run of a few classes, that wants its first slot and no more) or "wait"."""
    pids = {}
    rank = {"wait": 0, "small": 1, "prio": 2}
    try:
        names = [x for x in os.listdir(folder) if x.startswith(("wait-", "prio-"))]
    except OSError:
        return pids
    for name in names:
        path = os.path.join(folder, name)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:  # held: its waiter is alive
            with contextlib.suppress(ValueError, IndexError):
                pid = int(name.split("-")[1])
                kind = "prio" if name.startswith("prio-") else "small" if name.endswith("-s") else "wait"
                if rank[kind] >= rank[pids.get(pid, "wait")]:
                    pids[pid] = kind
        else:
            with contextlib.suppress(OSError):
                os.unlink(path)
        finally:
            os.close(fd)
    return pids


def _over_share(folder, n, ordinary, prio=False, snap=None):
    """T-4688: whether this run leaves a free ordinary slot to another run: it holds its share of the ordinary slots
    (fair_cap over the runs holding one or waiting for one) and another run that waits holds less than that share. A run
    holding none never refuses; a waiter at its share is not waited for, so no slot idles while two runs both want it. A
    priority run (the merge train's gate, T-3614) keeps at least half the ordinary slots as its share, so ten waiting
    builders do not cut it to a tenth.
    T-4789: a gate (a priority run, or the full suite another is queued behind on the suite lock) owes a waiting run its
    FIRST slot and no more: an ordinary waiter is entitled to one slot, not to the equal share (a ship's run of 3846 CPU-s
    on half the pool took its whole deadline; the full suite took 45 min beside builders). A waiter in a small run is
    entitled to its first slot whoever I am, and is not counted in the divisor (it asks for a reserved slot, not a share).
    A priority waiter keeps its half."""
    me = os.getpid()
    waiting = {p: kind for p, kind in _waiters(folder, snap).items() if p != me}
    with _YIELD_LOCK:
        for key in ("seen", "stuck"):  # a waiter that died is forgotten
            for pid in [p for p in _YIELD[key] if p not in waiting]:
                del _YIELD[key][pid]
    if not waiting:
        return False
    holders = _holders(folder, n, snap)
    if all(slot in holders for slot in ordinary):  # none free: nothing to leave (the clocks restart; a found-stuck waiter stays so)
        with _YIELD_LOCK:
            _YIELD["seen"].clear()
        return False
    held = {}
    for slot, pid in holders.items():
        if pid and slot in ordinary:
            held[pid] = held.get(pid, 0) + 1
    gate = prio or _gate_held(folder)
    cap = fair_cap(len(ordinary), len(set(held) | {p for p, k in waiting.items() if k != "small"} | {me}))
    half = -(-len(ordinary) // 2)
    runs = set(held) | {p for p, k in waiting.items() if k != "small"} | {me}

    def pshare(pid):
        # a priority run's share: half the pool, or every slot but one for each other run (its first slot): ten waiting
        # builders do not cut it to a tenth, and one short run beside it does not cost it a half
        return max(cap, half, len(ordinary) - len(runs - {pid}))
    own = pshare(me) if gate else cap
    if not gate and "prio" in waiting.values():
        own = 1  # beside a priority run still below its share I have my first slot and leave the rest
    if gate and "prio" not in waiting.values():
        # a gate beside waiters that are no priority runs keeps one free slot for a first slot and takes the rest: half
        # the pool for 5 streams of short runs was a run of 28.6 s that is 13.3 s on its own, and the ship's deadline
        own = 1
        if sum(1 for slot in ordinary if slot not in holders) > 1:
            return False
    if held.get(me, 0) < own:
        return False
    now, entitled = time.monotonic(), False
    with _YIELD_LOCK:
        for pid, kind in waiting.items():
            count = held.get(pid, 0)
            if count >= (pshare(pid) if kind == "prio" else 1 if gate or kind == "small" else cap):
                continue
            if _YIELD["stuck"].get(pid) == count:  # found stuck at this count: it stays so across full-then-free cycles
                continue
            _YIELD["stuck"].pop(pid, None)  # it took or let go a slot since: it is not the stuck waiter it was
            was = _YIELD["seen"].get(pid)
            if not was or was[0] != count:  # new, or it took a slot since: the clock starts again
                was = _YIELD["seen"][pid] = (count, now)
            if now - was[1] < FAIR_STUCK:
                entitled = True
            else:  # a waiter left a free slot this long and took none is alive but stuck
                _YIELD["stuck"][pid] = count
    return entitled


def tidy_slots(folder, n):
    """T-4688: remove the slot files beyond the pool that nobody holds (the pool shrank from 16 to 8 and left 8 to 15, and the
    express files x0 and x1 once the reserve took their place), and the temp files of a marker whose process died between
    creating it and renaming it: once per process and pool size. A file is removed only under its own exclusive lock, which
    a holder's lock refuses; _take checks the file it locked is still the one named. A slot file touched in the last
    TIDY_AGE s is left, and so is a temp file younger than TEMP_AGE. Residual: a run.py from before this change opens then
    locks without the inode check, so it can still take a file this removes in the moment between; the age keeps that to
    a file that sat idle, which a run of the old code takes only when its pool is the larger."""
    if (folder, n) in _TIDIED:
        return
    _TIDIED.add((folder, n))
    try:
        names = os.listdir(folder)
    except OSError:
        return
    wall = time.time()
    for name in names:
        slot = name.isascii() and name.isdigit() and str(int(name)) == name and int(name) >= n
        temp = name.startswith((".prio-", ".wait-", ".suite-"))
        if not (slot or temp or (name in EXPRESS and reserved(n))):
            continue
        path = os.path.join(folder, name)
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            if wall - os.fstat(fd).st_mtime < (TEMP_AGE if temp else TIDY_AGE):
                continue
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.unlink(path)
        except OSError:
            pass
        finally:
            os.close(fd)


def slot_holders(folder, n):
    """T-4708: {slot name: holder pid} of the slots held now, from the slots' own state on any system: a shared flock try
    on each (it fails exactly while another open file holds the slot, and is let go at once) and the pid the holder wrote
    in the file (0 if unreadable). The slots are the pool's 0..n-1 and the express files that exist."""
    held = {}
    for i in (*range(n), *EXPRESS):
        try:
            fd = os.open(os.path.join(folder, str(i)), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            try:
                held[i] = int(os.pread(fd, 32, 0).decode() or 0)
            except (OSError, ValueError):
                held[i] = 0
        os.close(fd)  # closing it lets go of a lock the try took
    return held


TRACE_ENV = "SAMPO_SLOT_TRACE"  # T-4685: =1: a run that waits prints what the slot decision read, every TRACE_EVERY s
TRACE_EVERY = 5.0
_TRACE, _TRACE_LOCK = {"at": None, "base": None}, threading.Lock()  # when the line was last said, the first throttle reading


def lock_holders(locks, inodes):
    """T-4685: {slot name: holder pid} from the text of /proc/locks and {slot name: inode}: the flock lines whose inode is
    a slot's. A pid the kernel shows as 0 or another namespace's is still a holder."""
    by_inode, held = {i: n for n, i in inodes.items()}, {}
    for ln in locks.splitlines():
        f = ln.split()
        if len(f) > 5 and f[1] == "FLOCK":
            try:
                ino = int(f[5].rsplit(":", 1)[1])
                if ino in by_inode:
                    held[by_inode[ino]] = int(f[4])
            except (ValueError, IndexError):
                continue
    return held


def _throttle():
    """T-4685: (periods throttled, periods) of this process's cgroup so far from cpu.stat, or None."""
    try:
        with open(os.path.join(_cgroup_dir() or "", "cpu.stat")) as f:
            d = dict(ln.split()[:2] for ln in f if ln.strip())
        return int(d["nr_throttled"]), int(d["nr_periods"])
    except (OSError, KeyError, ValueError):
        return None


def trace_line(waited, small, n, held, load, cpus, throttle=None, base=None, narrow=False):
    """T-4685: the one line SAMPO_SLOT_TRACE=1 prints on a refusal: what the decision read. held is {slot name: pid}; the
    reserved slots are the pool's highest numbers (lanes). load is the machine's 1-minute load, which the decision does
    not read: it is printed to set beside the holders."""
    r = reserved(n)
    ordinary = [s for s in held if isinstance(s, int) and s < n - r]
    small_only = [s for s in held if s not in ordinary]
    pids = sorted(set(held.values()))
    kind = "small" if small else "narrow" if narrow else "ordinary"
    line = (f"run.py: slot trace: waited {waited:.0f} s, {kind} run; pool {n} ({n - r} ordinary"
            f"{f', {r} small-only' if r else ', express files'}); held {len(ordinary)}/{n - r} ordinary, {len(small_only)} "
            f"small-only/express, by {len(pids)} run.py (pids {', '.join(map(str, pids)) or 'none'}); load {load:.1f} on "
            f"{cpus} CPUs (not read by the slot)")
    if throttle and base:
        dp = throttle[1] - base[1]
        line += f"; cgroup throttled {throttle[0] - base[0]} of {dp} periods since the first trace" if dp > 0 else ""
    return line + "\n"


def _trace(folder, n, small, waited, narrow=False):
    """T-4685: say what this refusal read (SAMPO_SLOT_TRACE=1), at most every TRACE_EVERY s per process."""
    with _TRACE_LOCK:
        now = time.monotonic()
        if _TRACE["at"] is not None and now - _TRACE["at"] < TRACE_EVERY:
            return
        _TRACE["at"] = now
        held = slot_holders(folder, n)  # T-4708: what the slots say, on any system; /proc/locks (Linux) refines the pids
        inodes = {}
        for i in held:
            with contextlib.suppress(OSError):
                inodes[i] = os.stat(os.path.join(folder, str(i))).st_ino
        try:
            with open("/proc/locks") as f:
                held.update(lock_holders(f.read(), inodes))
        except OSError:
            pass
        thr = _throttle()
        if _TRACE["base"] is None:
            _TRACE["base"] = thr
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
        sys.stderr.write(trace_line(waited, small, n, held, load, os.cpu_count() or 0, thr, _TRACE["base"], narrow))
        sys.stderr.flush()


PRIO_ENV = "SAMPO_SLOT_PRIORITY"  # T-3614: =1 (the train's gate and probes, ship's suite, sampo check outside a lane)


def _mark(folder, prefix="prio-", suffix=""):
    """T-3614: a waiting priority run's marker, (fd, path): locked before it takes its prio- name, so a prio- file
    nobody holds is a dead waiter's. None when it can't be made (the run then just competes). T-4570: prefix wait-
    is an ordinary waiting run's, made the same way. T-4789: suffix -s marks a small run's wait-; the prefix suite- is a
    full suite waiting on the machine's suite lock."""
    name = f"{prefix}{os.getpid()}-{random.getrandbits(32):08x}{suffix}"
    tmp, path = os.path.join(folder, "." + name), os.path.join(folder, name)
    try:
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.rename(tmp, path)
    except OSError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return None
    return fd, path


LOW_ENV = "SAMPO_SLOT_LOW"  # T-4570: =1 (the post-merge verify's runs): their classes yield freed slots to waiting runs
LOW_MAX = 600.0  # T-4570: a low run.py yields to waiting runs for this long from its first yield, then competes as any run
_LOW, _LOW_LOCK = {"until": None}, threading.Lock()  # T-4570: when this process's window to yield ends (None: not begun)
_WAIT, _WAIT_LOCK = {"n": 0, "mark": None}, threading.Lock()  # T-4570: this process's waiting classes and their one marker


def is_low():
    """T-4570: whether this run is the post-merge verify's (SAMPO_SLOT_LOW=1): its classes yield to waiting runs."""
    return _env(LOW_ENV) == "1"


def _wait_enter(folder, small=False):
    """T-4570: a class of this process starts waiting for a slot: the process's one wait- marker is made for the first.
    True when counted (the caller then calls _wait_leave); the marker may be missing (the verify just doesn't yield).
    T-4789: a small run's marker says so (-s), so a long run does not count it as one of the runs sharing the slots."""
    with _WAIT_LOCK:
        _WAIT["n"] += 1
        if not _WAIT["mark"]:
            _WAIT["mark"] = _mark(folder, "wait-", "-s" if small else "")
    return True


def _wait_leave():
    """T-4570: a waiting class got its slot or gave up; the last one out removes the marker."""
    with _WAIT_LOCK:
        _WAIT["n"] -= 1
        if _WAIT["n"] <= 0 and _WAIT["mark"]:
            fd, path = _WAIT["mark"]
            _WAIT["mark"] = None
            with contextlib.suppress(OSError):
                os.unlink(path)
            os.close(fd)


def _builder_waiting(folder, ordinary=None, n=0, snap=None):
    """T-4570: whether a low class leaves a freed slot to a waiting ordinary run: one holds a wait- marker and this
    process's window (LOW_MAX from its first yield, one window for every class of the run) is still open.
    T-4789: only to a run that holds no slot yet (its first), and not while a run is queued behind this one's full suite
    on the suite lock: a run waiting with five slots of its own was a reason to yield, and the verify sat at zero jobs
    for 387 of 429 s beside a stream of short runs, free slots under it the whole time. `ordinary` are the pool's slots a
    holder is counted on (None: the old rule, any wait- marker)."""
    with _LOW_LOCK:
        if _LOW["until"] is not None and time.monotonic() >= _LOW["until"]:
            return False
    if ordinary is not None:
        if _gate_held(folder):
            return False
        # (this process's own marker counts: a run's classes are all low or none is, so it holds one only for a thread that
        # is not low, and that thread's first slot is as much a first as another run's. The invariant it relies on: a low
        # process never writes a wait- marker, cpu_slot's `elif not prio and not low and not marked` makes one only for a
        # class that is not low, so it can never yield to itself; letting a low class write one would recreate the self-yield)
        waiting = {p for p, kind in _waiters(folder, snap).items() if kind != "prio"}
        if not waiting:
            return False
        held = _holders(folder, n, snap)
        needy = waiting - {pid for slot, pid in held.items() if slot in ordinary}
        if not needy:
            return False  # every waiting run holds a slot already: nobody here waits for a first one
        if sum(1 for slot in ordinary if slot not in held) > len(needy):
            return False  # a free slot for each run waiting for its first, and one more: that one is mine
    elif not _prio_waiting(folder, "wait-"):
        return False
    with _LOW_LOCK:
        if _LOW["until"] is None:
            _LOW["until"] = time.monotonic() + LOW_MAX
    return True


def _prio_waiting(folder, prefix="prio-", suffix=""):
    """T-3614: whether a priority run waits for a slot (holds a prio- marker); a dead waiter's marker is removed.
    T-4570: prefix wait- asks the same of ordinary waiting runs. T-5050: suffix GATE_SUFFIX asks it of a gate alone."""
    try:
        names = [x for x in os.listdir(folder) if x.startswith(prefix) and x.endswith(suffix)]
    except OSError:
        return False
    for name in names:
        path = os.path.join(folder, name)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)
    return False


def _slot_folder():
    """The pool's folder, made if missing: None unless it's a real folder of mine that nobody else may write (a
    squatted /tmp path or an unwritable XDG_RUNTIME_DIR runs the class without a slot)."""
    uid = getattr(os, "getuid", lambda: 0)()
    folder = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), f"sampo-slots-{uid}")
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        st = os.lstat(folder)
    except OSError:
        return None
    import stat
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o022:
        return None
    return folder


SUITE_ENV = "SAMPO_NO_SUITE_SLOT"  # T-3541: =1 runs a full suite without the machine-wide slot
_SUITE = {"held": False}  # T-4789: this process holds the machine's suite lock now (its full suite is running)


def _gate_held(folder):
    """T-4789: whether this run is a gate: it holds the machine's suite lock and another full suite is queued for it (a
    suite- marker, made while that run waits on the lock): the run everything behind it waits for, so it keeps its slots
    beyond a waiting run's first, as a priority run does."""
    return _SUITE["held"] and _prio_waiting(folder, "suite-")


def _holder(fd):
    """Who holds the suite slot, from what it wrote into the lock file."""
    try:
        h = json.loads(os.pread(fd, 4096, 0))
        return f"the full suite running in {h['cwd']} (pid {int(h['pid'])}, {(time.time() - float(h['at'])) // 60:.0f} min)"
    except (OSError, ValueError, TypeError, KeyError):  # not written yet, or garbled
        return "another full suite"


@contextlib.contextmanager
def suite_slot(until):
    """T-3541: the machine's one full-suite slot, held while the block runs: three full suites at once (two builders'
    and the ship's) each took 3x as long and slowed everything else. A second full run waits, saying for whom every
    minute, and goes ahead beside the holder once until (an epoch) passes. None when nested in a class (SAMPO_SLOT),
    SAMPO_NO_SUITE_SLOT=1 or no flock. The holder's pid, cwd and start are in the lock file; the kernel frees the lock
    when the holder dies."""
    fd = None
    if not (_env(SLOT_ENV) or _env(SUITE_ENV) or not fcntl):
        path = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "sampo", "suite.lock")
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    said, mark = 0.0, None
    try:
        while fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass
            else:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)
                    os.pwrite(fd, json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "at": time.time()}).encode(), 0)
                _SUITE["held"] = True
                break
            now = time.time()
            if ENDED:  # T-4399: ended from outside: no suite starts, the wait ends now
                break
            if now >= until:
                sys.stderr.write(f"run.py: {_holder(fd)} still holds the slot; going ahead beside it\n")
                break
            if now - said >= 60:
                sys.stderr.write(f"run.py: waiting for {_holder(fd)}\n")
                sys.stderr.flush()
                said = now
            if not mark:  # T-4789: the holder sees a full suite is queued behind it (it keeps its slots: _gate_held)
                folder = _slot_folder() if fcntl else None
                mark = _mark(folder, "suite-") if folder else None
            time.sleep(0.2)  # the next suite starts within 0.2 s of the slot freeing
        if mark:
            _unmark(mark)
            mark = None
        yield
    finally:
        _SUITE["held"] = False
        if mark:
            _unmark(mark)
        if fd is not None:
            os.close(fd)  # closing it frees the slot


def skipped():
    """T-1101: the modules FOREMAN_SKIP_TESTS names (fm check: another open task's red-first test files)."""
    return {x.strip() for x in os.environ.get("FOREMAN_SKIP_TESTS", "").split(",") if x.strip()}


SPLIT_S = 5.0  # T-3166: a class whose last run took this long runs a job per test
START_S = 0.3  # ... what each job's interpreter start-up costs, left out of a split class's work time
# Classes seen slow (tonight's measurements): split with no times yet (a first run, a --shard), where every other class
# stays one job (an interpreter per test of 1400 classes would swamp a 2-core runner).
SEEDED = {"LeanKind", "Stop", "ExamAuthors", "ExamGen", "Page", "ClockWindows", "PreToolUse", "SessionStart",
          "Worker", "GenesisRead"}


def keeps_whole(t):
    """T-3166: a class with setUpClass/tearDownClass, a module with setUpModule/tearDownModule, a module that won't
    import, or a class marked `run_whole = True` shares state between its tests: one job."""
    c, tc = type(t), unittest.TestCase
    m = sys.modules.get(c.__module__)
    own = lambda n: getattr(getattr(c, n, None), "__func__", None) is not getattr(tc, n).__func__  # noqa: E731  (None: whole)
    return (isinstance(t, unittest.loader._FailedTest) or getattr(c, "run_whole", False)
            or own("setUpClass") or own("tearDownClass")
            or any(hasattr(m, n) for n in ("setUpModule", "tearDownModule")))


def legacy_times_path():
    """T-3166's file: one per tree in the temp folder, lost on a reboot. T-4268: only read, never written."""
    return os.path.join(
        tempfile.gettempdir(), f"fmtest-times-{getattr(os, 'getuid', lambda: 0)()}-{hashlib.sha1(os.fsencode(HERE)).hexdigest()[:10]}.json")


def times_path():
    """T-4268: the repo's own file in the shared run-cache folder, so a worktree made today schedules by the repo's
    history; the legacy temp file outside git."""
    if env := os.environ.get("FM_TEST_TIMES"):
        return env
    common = _common()
    return os.path.join(_cache_dir(), repo_id(common) + ".times.json") if common else legacy_times_path()


def _read_times(path):
    try:
        with open(path) as f:
            if os.fstat(f.fileno()).st_uid != getattr(os, "getuid", lambda: 0)():  # someone else's file in a shared /tmp
                return {}
            d = json.load(f)
        return {k: float(v) for k, v in d.items()}
    except FileNotFoundError:
        return None
    except Exception:  # OverflowError, RecursionError ...: times only order the jobs
        return {}


def load_times():
    """T-3166: seconds each job took the last time it ran ({job id: s}); {} when there is none yet. T-4268: while the
    shared file is missing, the legacy temp file's."""
    got = _read_times(times_path())
    if got is None and not os.environ.get("FM_TEST_TIMES"):
        got = _read_times(legacy_times_path())
    return got or {}


def estimate(names, times, slots):
    """T-4892: {seconds, known, slots}: what the selection `names` (modules, or Module.Class) is known to cost from
    `times` (load_times: job id -> seconds), over `slots` CPU slots. A class run whole and, another time, split into its
    tests has both kinds of key: it counts as the larger of the two. Classes with no record add nothing (known counts
    the ones that do), so the caller never sizes below its own floor. One pass over the times."""
    names = [n.replace("::", ".").rstrip(".") for n in names]  # T-5588: parse's "mod.Class." and `mod::Class` are Module.Class
    mods = {n for n in names if "." not in n}
    named = {n for n in names if "." in n}
    per = {}
    for k, v in times.items():
        parts = k.split(".")
        cls = ".".join(parts[:2])
        if parts[0] not in mods and cls not in named and k not in named:
            continue
        w = per.setdefault(cls, [0.0, 0.0])
        if len(parts) <= 2:
            w[0] = max(w[0], float(v))
        else:
            w[1] += float(v)
    return {"seconds": round(sum(max(a, b) for a, b in per.values()), 1), "known": len(per), "slots": slots}


def save_times(new):
    """Merged into the file's, so a run of a few modules keeps the rest's; capped, newest kept."""
    path = times_path()
    with _cache_lock(path):  # T-4268: every worktree of the repo writes this file: the read-merge-write is one step
        d = load_times()
        for k, v in new.items():
            d.pop(k, None)
            d[k] = round(v, 2)
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(path), exist_ok=True)  # the shared folder may not exist yet
        _write_json(path, dict(list(d.items())[-20000:]))


def _write_json(path, d):
    p = f"{path}.{os.getpid()}"
    try:
        with contextlib.suppress(OSError):
            os.unlink(p)  # a stale or planted one: O_EXCL below then never follows a symlink
        with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600), "w") as f:
            json.dump(d, f)
        os.replace(p, path)  # atomic: a run beside this one reads the old file or the new, never half
    except OSError:
        pass  # times only order the jobs, and the cache only saves reruns


NO_CACHE = "SAMPO_NO_TEST_CACHE"  # T-3502: =1 (or FOREMAN_NO_TEST_CACHE) runs every class, cached or not
CACHE_FULL = "SAMPO_CACHE_FULL"  # T-3638: =1 lets a full run (no names) skip cached classes; only the post-merge verify sets it
CLASS_IDS = True  # T-5588: names may be `module::Class` (parse): sampo check --affected reads this word to name classes
SAMPLE = 0.1 # the share of cached classes rerun anyway: a file the coverage missed shows, and the cache is dropped
STATE = ".sampo/"  # Sampo's synced task state: every sampo call writes it, no test reads it (each has its own home)
PRUNE_S = 14 * 86400  # a repo's cache unused this long (a removed lane's) is deleted
KEEP = 3  # T-3593: records kept per class, newest first: main's code beside the lanes' that changed what it reads
COVERED = {}  # T-3593: job id -> what its processes recorded (runcover.py's lines), filled by _run_one
CPU = {}  # T-3757: job id -> the CPU s its processes took (BOOT's file), filled by _run_one
WAITED = {}  # T-4247: job id -> the s its class waited for a slot (run_one), not its run time: nothing else keeps it


WAIT_SPANS = []  # T-4582: (start, end) in time.monotonic() of each class's wait for a slot: wall_wait() takes their union


HELD = {}  # T-4570: job id -> the s its class held a slot (run_one): the machine's slot-seconds this run took


def slot_kind():
    """T-4570: the kind of holder this run is, as its record says: a gate (SAMPO_SLOT_PRIORITY=1), the post-merge verify
    (SAMPO_SLOT_LOW=1) or any other run (a builder's, the main thread's)."""
    return "priority" if _env(PRIO_ENV) == "1" else "low" if is_low() else "normal"


def slot_fields(started, ended):
    """T-4570: what a run's record says about the slots: when it started and how long it took (wall), the s its classes
    waited for a slot and held one (sums over its worker threads: with -j N they add up to about N times the wall),
    and its kind. Waiting was counted in the summary line only, and the holder nowhere: 2026-11-13's 'who held the
    slots' could not be read back from disk."""
    return {"started": round(started, 3), "seconds": round(ended - started, 1), "waited_s": round(sum(WAITED.values()), 1),
            "waited_wall_s": round(wall_wait(WAIT_SPANS), 1),  # T-4582: the wall-clock wait, not summed over threads
            "held_s": round(sum(HELD.values()), 1), "kind": slot_kind()}


def held_by_kind(recs, day):
    """T-4570: ({kind: {runs, held_s, waited_s, seconds}}, n records of that UTC day without slot fields) over run
    records: the slot-seconds each kind of holder took on `day` (YYYY-MM-DD)."""
    out, unrecorded = {}, 0
    for r in recs:
        if not isinstance(r, dict) or time.strftime("%Y-%m-%d", time.gmtime(r.get("t") or 0)) != day:
            continue
        if "held_s" not in r:
            unrecorded += 1
            continue
        k = out.setdefault(str(r.get("kind") or "normal"), {"runs": 0, "held_s": 0.0, "waited_s": 0.0, "seconds": 0.0})
        k["runs"] += 1
        for f in ("held_s", "waited_s", "seconds"):
            k[f] = round(k[f] + float(r.get(f) or 0), 1)
    return out, unrecorded


def show_known_red(a):
    """--known-red CLASS...: {class: age in s} as JSON of the named classes whose newest run record, inside the window, is
    red on the inputs this tree gives (known_red's own rule). Runs nothing, takes no slot; `{}` and exit 3 outside git."""
    got = tree()
    if not got:
        print("{}")
        sys.stderr.write("run.py: not in a git repository: no run records\n")
        return 3
    try:
        print(json.dumps(known_red(got[2], set(a.known_red), values(*got[:2]), known_red_window()), sort_keys=True))
    except Exception as e:  # a record that can't be read is no word on the class
        print("{}")
        sys.stderr.write(f"run.py: the run records were not read: {repr(e)[:200]}\n")
        return 1
    return 0


def show_held(a):
    """--held [--day YYYY-MM-DD]: the slot-seconds each kind of run took that UTC day (default today), from this repo's
    run records (the newest KEEP_RUNS are kept, so an older day may be partly gone). Runs nothing, takes no slot."""
    where = _where()
    if not where:
        sys.stderr.write("run.py: not in a git repository: no run records\n")
        return 3
    day = a.day or time.strftime("%Y-%m-%d", time.gmtime())
    folder, recs = runs_dir(where[1]), []
    try:
        names = sorted(n for n in os.listdir(folder) if RUN_NAME.fullmatch(n))
    except OSError:
        names = []
    for n in names:
        try:
            with open(os.path.join(folder, n)) as f:
                recs.append(json.load(f))
        except (OSError, ValueError):
            continue
    got, unrecorded = held_by_kind(recs, day)
    for kind, v in sorted(got.items(), key=lambda kv: -kv[1]["held_s"]):
        print(f"{kind}: held {v['held_s']:.0f} slot-s, waited {v['waited_s']:.0f} s, {v['runs']} runs, "
              f"{v['seconds']:.0f} s wall (waited and held add up over each run's worker threads)")
    if not got:
        print(f"no run record of {day} has slot fields (runs before T-4570 did not write them)")
    if unrecorded:
        print(f"{unrecorded} runs of {day} wrote no slot fields")
    return 0


def wall_wait(spans):
    """T-4582: the seconds of wall clock at least one job spent waiting for a slot: the length of the union of the
    (start, end) spans, so two jobs waiting at once count once. An empty or backwards span counts nothing."""
    total, end = 0.0, None
    for a, b in sorted(s for s in spans if s[1] > s[0]):
        if end is None or a > end:
            total, end = total + (b - a), b
        elif b > end:
            total, end = total + (b - end), b
    return total


def waited_note(waits, spans=(), run_s=0.0):
    """T-4247, T-4582: the summary's '; waited W s of R s wall for a slot (P%), S s summed over jobs' when some job was
    waiting for a slot over 5 s of the run's wall clock, else ''. W is the union of the wait spans, R the run's wall;
    S adds each worker thread's wait (with -j N about N times W) and is said last, as the sum it is: it read as hours."""
    wall = wall_wait(spans)
    if wall <= 5:
        return ""
    run_s = max(run_s, wall)  # a clock skew between the run's start and the spans must not read over 100%
    return (f"; waited {wall:.0f} s of {run_s:.0f} s wall for a slot ({100 * wall / run_s:.0f}%), "
            f"{sum(waits.values()):.0f} s summed over jobs")
BUDGETS = {}  # T-3757: class id -> its CPU_BUDGET_S, a costly new class's declared cost
READS = {}  # T-5484: class id -> its module's RUN_READS, the files its run reads when only a named browser's '*' is left
CPU_BASE, CPU_GATE = "SAMPO_CPU_BASE", "SAMPO_CPU_GATE"  # T-3757: the train's baseline file; =1 fails a rise
CPU_FLOOR = "SAMPO_CPU_FLOOR_S"  # T-3757: the absolute margin, 20 CPU s; a new class may take 4.5x it, a run 6x
CPU_RATIO, CPU_SUM_RATIO, CPU_KEEP, CPU_UNSEEN_DAYS = 2.0, 1.5, 9, 14
GIT_STATE = "\0git"  # tree()'s key for HEAD and the tags (a recorded "g": git run on the repo); no path is named so,
# nor any of its other "\0" keys (T-3691)


def _cache_base():
    return os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")


def _cache_dir():
    return os.path.join(_cache_base(), "sampo", "run-cache")


def repo_id(common):
    return hashlib.sha1(os.fsencode(common)).hexdigest()[:16]


def cache_path(repo):
    return os.path.join(_cache_dir(), repo_id(repo) + ".json")


_COMMON = {}


def _where():
    """(this worktree's root, the repo's shared git folder as tree() names it: the same for every worktree), read
    without hashing the tree; None outside git."""
    if HERE not in _COMMON:
        try:
            repo = _git(HERE, "rev-parse", "--show-toplevel").strip()
            _COMMON[HERE] = (os.path.realpath(repo),
                             os.path.realpath(os.path.join(repo, _git(repo, "rev-parse", "--git-common-dir").strip())))
        except (OSError, subprocess.SubprocessError):
            _COMMON[HERE] = None
    return _COMMON[HERE]


def _common():
    """The repo's shared git folder; None outside git."""
    return (_where() or (None, None))[1]


def in_lane(here=None):
    """T-5588: True in a linked worktree (a lane): its own git dir (`rev-parse --git-dir`) is a sub-folder of the shared
    one (`--git-common-dir`), not the shared one itself. False in the main checkout, a submodule or a checkout whose
    git dir lives elsewhere (both have the two equal: not `.git` being a file) and outside git."""
    here = here or HERE
    try:
        own, shared = (os.path.realpath(os.path.join(here, _git(here, "rev-parse", flag).strip()))
                       for flag in ("--git-dir", "--git-common-dir"))
    except (OSError, subprocess.SubprocessError):
        return False
    return own != shared


def sample_rate(full):
    """T-5588: the share of cache hits rerun anyway. A lane's run is one selected class set the accept check reruns
    in full after the merge, so it skips the resample; the full gate (SAMPO_CACHE_FULL, the post-merge verify) and any
    run outside a lane keep it: a coverage miss still shows there."""
    return SAMPLE if full or not in_lane() else 0.0


# T-4268: one JSON per run under <repo id>.runs/, named by end time then pid (a whole-suite run's with its worktree's
# hash): what ran, on which tree, which classes failed and how; `run.py --last` prints the newest one written from this
# worktree. Green runs too: "this selection was green here".
KEEP_RUNS, KEEP_WHOLE = 200, 100  # the newest runs, and beyond them the newest whole-suite run of up to 100 worktrees
TEXT_MAX, FAILS_MAX, PER_CLASS, NOT_RUN_MAX = 2000, 60, 10, 50
RUN_NAME = re.compile(r"(\d{20})-(\d+)(?:-w([0-9a-f]{8}))?\.json")
RUN_TEMP = re.compile(r"\d{20}-\d+(?:-w[0-9a-f]{8})?\.json\.\d+")  # _write_json's temp file, left by a crash
TEMP_S = 3600
RUN_ID_ENV = "SAMPO_TEST_RUN_ID"  # T-4268: the check's id for its gate; the record carries it, no class or child run gets it


def runs_dir(common):
    return os.path.join(_cache_dir(), repo_id(common) + ".runs")


def tree_id(blobs):
    """The files' content alone (not HEAD or the tags, not Sampo's state, which every sampo call rewrites): a commit of
    what was tested is still that tree."""
    return hashlib.sha1("".join(f"{p}\0{b}\n" for p, b in sorted(blobs.items()) if p[:1] != "\0" and not p.startswith(STATE)
                                ).encode(errors="surrogateescape")).hexdigest()[:16]


def failures(cid, err):
    """[{class, test, first, text}] for a failed class's output: one per FAIL:/ERROR: block, `first` the exception's
    line (else the block's last), `text` the block's last TEXT_MAX characters; output with no block (an import error,
    a kill) is one entry with its last line."""
    out = []
    for name, block in re.findall(r"(?m)^(?:FAIL|ERROR): (\w+) \([^\n]*\)\n-+\n(.*?)(?=\n=+\n|\n-+\nRan |\Z)", err, re.S):
        lines = [x for x in block.splitlines() if x.strip()]
        exc = next((x for x in lines if re.match(r"(?:[\w.]+\.)?\w*(?:Error|Exception|Failure|Interrupt)\b", x)), None)
        out.append({"class": cid, "test": name, "first": (exc or (lines[-1] if lines else "")).strip()[:300],
                    "text": block.strip()[-TEXT_MAX:]})
    if not out:
        tail = [x.strip() for x in err.splitlines() if x.strip() and not set(x.strip()) <= set("-=~^")]
        out.append({"class": cid, "test": "", "first": (tail[-1] if tail else "no output")[:300],
                    "text": err.strip()[-TEXT_MAX:]})
    return out[:PER_CLASS]


def fail_entries(results, owner, cpu_red):
    """The failures() of each failed job, each under its class (owner: a split class's job is one test of it), then a
    class entry for each CPU-over-margin red class."""
    out = []
    for job, _, err, _, _ in results:
        cid = owner(job)
        for f in failures(cid, err):
            if not f["test"] and job != cid and job.startswith(cid + "."):
                f["test"] = job[len(cid) + 1:]  # a kill or an import error of one test's job: that test
            out.append(f)
    return out + [{"class": c, "test": "", "first": f"CPU over its margin (T-3757): {s:.1f} CPU s, {why}", "text": "",
                   "own": True} for c, s, _, why in cpu_red]


def _redactor():
    """Sampo's redactor, imported only now (a failure to write down) from the lib beside the tests folder; None for a
    copy of run.py alone (test_check_paths), where nothing is known to be safe to keep."""
    lib = os.path.join(os.path.dirname(HERE), "lib")
    if not os.path.isdir(os.path.join(lib, "sampo")):
        return None
    try:
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from sampo import core
        return core.redact
    except Exception:
        return None


def safe_entries(entries, redact):
    """The entries as they may be written: failure text through `redact`; with none (or one that raises), the class and
    test names only. A line run.py made itself (`own`, the CPU margin) is kept."""
    out = []
    for e in entries:
        e = dict(e)
        own = e.pop("own", False)
        if not own:
            try:
                e["first"], e["text"] = ((redact(e.get("first", ""))[:300], redact(e.get("text", ""))) if redact
                                         else ("", ""))
            except Exception:
                e["first"], e["text"] = "", ""
        out.append(e)
    return out


def record_run(common, rec):
    """The run's record written whole under its own name (atomic), then the folder pruned."""
    folder = runs_dir(common)
    with contextlib.suppress(OSError):
        os.makedirs(folder, exist_ok=True)
    wt = rec.get("worktree")
    tag = f"-w{hashlib.sha1(os.fsencode(wt)).hexdigest()[:8]}" if rec.get("whole") and wt else ""
    _write_json(os.path.join(folder, f"{time.time_ns():020d}-{os.getpid()}{tag}.json"), rec)
    prune_runs(folder)


def prune_runs(folder):
    """Records past the newest KEEP_RUNS removed, except the newest whole-suite one of each worktree (a pile of -k runs
    must not push out the one red full run); a crashed write's temp file over an hour old removed. Only names this
    module makes: a folder beside them (running/) or a file another writer adds is left alone."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    recs = sorted(n for n in names if RUN_NAME.fullmatch(n))
    keep, wholes = set(recs[-KEEP_RUNS:]), set()
    for n in reversed(recs):
        w = RUN_NAME.fullmatch(n).group(3)
        if w and w not in wholes and len(wholes) < KEEP_WHOLE:
            wholes.add(w)
            keep.add(n)
    now = time.time()
    for n in names:
        with contextlib.suppress(OSError):
            q = os.path.join(folder, n)
            if (n in recs and n not in keep) or (RUN_TEMP.fullmatch(n) and now - os.lstat(q).st_mtime > TEMP_S):
                os.unlink(q)


def last_record(common, tree=None, worktree=None, whole=None, run_id=None):
    """The newest record that matches every filter given, or None: `tree` its content id at the start or the end of
    the run, `worktree` the path it was written from, `whole` a whole-suite run, `run_id` the check's id."""
    folder = runs_dir(common)
    try:
        names = sorted((n for n in os.listdir(folder) if RUN_NAME.fullmatch(n)), reverse=True)
    except OSError:
        return None
    if whole:  # the names say it: no need to read the others
        names = [n for n in names if RUN_NAME.fullmatch(n).group(3)]
    for n in names:
        try:
            with open(os.path.join(folder, n)) as f:
                rec = json.load(f)
        except (OSError, ValueError):  # half-written or removed by a run beside this one: the next
            continue
        if (isinstance(rec, dict)
                and (tree is None or (tree and tree in (rec.get("tree"), rec.get("tree_end"))))
                and (worktree is None or rec.get("worktree") == worktree)
                and (not whole or rec.get("whole"))
                and (run_id is None or rec.get("run_id") == run_id)):
            return rec
    return None


KNOWN_RED_MIN = "SAMPO_KNOWN_RED_MIN"  # T-4269: minutes a record of a red class is believed (default 60); 0: no reordering
KNOWN_RED_DEFAULT_MIN = 60.0
KNOWN_CLASSES_MAX, KNOWN_PATHS_MAX, KNOWN_PATH_LEN = 10, 1500, 400  # what a record keeps of a red class's inputs


def known_red_window():
    """T-4269: seconds a record of a red class is believed; 0 turns known-red-first off (a bad value: the default)."""
    raw = _env(KNOWN_RED_MIN)
    try:
        minutes = float(raw) if raw not in (None, "") else KNOWN_RED_DEFAULT_MIN
    except ValueError:
        minutes = KNOWN_RED_DEFAULT_MIN
    return minutes * 60 if minutes > 0 else 0.0  # a negative or NaN one is off, never "forever"


def red_inputs(failed_classes, results, owner, base, repo, blobs, value):
    """T-4269: {class: [digest, paths]} for the red classes of a run: the paths its jobs read (what the cache records
    for a green class, shared files included) and their digest on the tree the run started on. A class whose reads
    went unrecorded, or too many of them, is left out: nothing is then known of its inputs. At most KNOWN_CLASSES_MAX."""
    out = {}
    for cid in list(dict.fromkeys(failed_classes))[:KNOWN_CLASSES_MAX]:
        texts = [COVERED.get(r[0]) for r in results if owner(r[0]) == cid]
        if not texts or None in texts:
            continue
        paths = sorted(run_paths(base, texts, repo, blobs, READS.get(cid)))  # T-5484: the declared reads, as the cache's
        if len(paths) <= KNOWN_PATHS_MAX and all(len(p) <= KNOWN_PATH_LEN for p in paths):
            out[cid] = [digest(paths, value), paths]
    return out


def known_red(common, cids, value, window, now=None):
    """T-4269: {class: age in s} for each of cids whose newest word in the repo's run records, within `window` s, is
    'red' on the inputs the current tree gives: the record's paths read on this tree (`value`) digest to the one the
    record kept (the digest the green cache keys on). A newer record naming the class red without inputs, or
    'cleared' (red once, then green on the same inputs: flaky), or on other inputs, ends the search: not known red.
    Reads only the records inside the window, newest first; any it can't read is skipped."""
    now = time.time() if now is None else now
    out, left = {}, set(cids)
    if window <= 0 or not left:
        return out
    try:
        names = sorted((n for n in os.listdir(runs_dir(common)) if RUN_NAME.fullmatch(n)), reverse=True)
    except OSError:
        return out
    for n in names:
        if not left or now - int(n[:20]) / 1e9 > window:  # names sort by time: every later one is older still
            break
        try:
            with open(os.path.join(runs_dir(common), n)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        t = rec.get("t") if isinstance(rec, dict) else None
        if not isinstance(t, (int, float)) or now - t > window:
            continue
        inputs = rec.get("inputs") if isinstance(rec.get("inputs"), dict) else {}
        named = {f.get("class") for f in rec.get("failed") or [] if isinstance(f, dict)}
        named |= set(x for x in rec.get("cleared") or [] if isinstance(x, str))
        for cid in sorted(left & (named | set(inputs))):
            left.discard(cid)
            got = inputs.get(cid)
            if (isinstance(got, list) and len(got) == 2 and isinstance(got[0], str) and isinstance(got[1], list)
                    and len(got[1]) <= KNOWN_PATHS_MAX and all(isinstance(p, str) and len(p) <= KNOWN_PATH_LEN for p in got[1])
                    and digest(got[1], value) == got[0]):
                out[cid] = max(0.0, now - t)
    return out


def record_lines(rec):
    """`red at <time>: <class>.<test>: <first>` per failure, or `green at <time> (N tests, M more green in the cache;
    selection: <what was asked>)`."""
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec.get("t") or 0))
    if rec.get("ok"):
        bits = [f"{rec.get('ran', 0)} tests"] + ([f"{rec['cached']} more green in the cache"] if rec.get("cached") else []) \
            + ([f"joined {rec['joined']}"] if rec.get("joined") else [])  # T-5557: the classes another run's green vouched for
        return [f"green at {at} ({', '.join(bits)}{'; selection: ' + rec['selection'] if rec.get('selection') else ''})"]
    out = [f"red at {at}: {f['class']}{'.' + f['test'] if f.get('test') else ''}{': ' + f['first'] if f.get('first') else ''}"
           for f in rec.get("failed") or []]
    if rec.get("not_run"):
        out.append(f"red at {at}: stopped at its deadline, {rec.get('n_not_run') or len(rec['not_run'])} classes not run")
    return out or [f"red at {at}: no failure line recorded"]


def show_last(a):
    """--last: print the newest record written from this worktree (no tree read), its short tree id after it; --same-tree
    the newest of this content (reads the tree), --any-tree of the repo, --run-id the one stamped by that check's id.
    Runs nothing, takes no slot. A green selection is followed by the worktree's newest whole-suite run when that is red."""
    where = _where()
    rec, what = None, "this worktree"
    if where and a.run_id:
        rec, what = last_record(where[1], run_id=a.run_id), f"run id {a.run_id}"
    elif where and a.any_tree:
        rec, what = last_record(where[1]), "any worktree of this repo"
    elif where and a.same_tree:
        got = tree()
        rec, what = (last_record(where[1], tree=tree_id(got[1])) if got else None), "this tree's content"
    elif where:
        rec = last_record(where[1], worktree=where[0])
    if not rec:
        sys.stderr.write(f"run.py: no record of a run for {what}\n")
        return 3
    if a.json:
        print(json.dumps(rec, indent=1))
        return 0
    out = record_lines(rec) + [f"tree {rec['tree'][:12]}" if rec.get("tree") else "tree not read (found by worktree path)"]
    if rec.get("ok") and not rec.get("whole") and rec.get("worktree"):
        old = last_record(where[1], worktree=rec["worktree"], whole=True)
        if old and not old.get("ok"):
            out += [f"{x} (newest whole-suite run)" for x in record_lines(old)[:5]]
    print("\n".join(out))
    return 0


def load_cache(path):
    """(generation, {class id: [[digest, tests, [path, ...], cpu?], ...]}): per class up to KEEP records of a whole green
    run, each the paths it read and their digest then (T-3593), and the CPU s it took when known (T-3757); (0, {}) when
    missing or corrupt. The generation goes up
    each time a run finds the cache wrong and drops it (T-3502). On disk the paths are indexes into one table."""
    try:
        with open(path) as f:
            d = json.load(f)
        table = d.get("paths", [])
        # ponytail: whole file parsed and indexes resolved each run (~0.1-0.3 s at the full suite's ~1500 classes x 3);
        # store the indexes as strings and decode only the named classes' if that shows in a profile
        recs = {c: [[v[0], int(v[1]), [table[i] for i in v[2]], *v[3:4]] for v in vs] for c, vs in d["classes"].items()}
        return int(d["gen"]), recs
    except Exception:  # a corrupt cache, or T-3502's keys, only costs reruns
        return 0, {}


def load_why(path):
    """{class id: cause} kept beside the records in the file's "why" (T-5484): the cause of a whole-tree record, what
    its run read past (why_of); {} when none is kept or the file is unreadable."""
    try:
        with open(path) as f:
            why = json.load(f).get("why", {})
        return {str(c): str(w) for c, w in why.items()} if isinstance(why, dict) else {}
    except Exception:
        return {}


def why_of(texts):
    """The cause of a whole-tree record (T-5484): the first "* " line of the cover texts its run wrote, runcover's
    "* {why} {command}", cut to 120 characters; None when no text has one."""
    for t in texts:
        for line in (t or "").splitlines():
            if line.startswith("* "):
                return line[2:122]
    return None


def causes(cov, new):
    """{class id: why_of(its cover texts)} for the whole-tree records in new (T-5484): those whose paths hold "*"."""
    out = {}
    for c, r in new.items():
        w = why_of(cov.get(c, ())) if "*" in r[2] else None
        if w:
            out[c] = w
    return out


@contextlib.contextmanager
def _cache_lock(path):
    """The cache's lock file held, so a read-merge-write is one step among the runs beside this one; no lock without
    flock (Windows) or a folder it can't write."""
    fd = None
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.utime(path + ".lock")  # in use: another repo's save never prunes it from under its holder
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)  # closing it frees the lock


def save_cache(path, new, gen, why=None):
    """New records ({class id: [digest, tests, paths]}) merged into the file's under its lock, each first among its
    class's KEEP, newest 20000 classes kept; nothing when the cache was dropped since this run read it at gen (its
    records came from a cache found wrong). The other repos' caches unused for PRUNE_S go. why ({class id: cause},
    T-5484) is kept beside the records; a class's old cause goes when its record is replaced and why has none for it."""
    with _cache_lock(path):
        cur_gen, recs = load_cache(path)
        if cur_gen != gen:
            names = sorted(new)  # T-5484: say which classes' records were dropped (no behaviour change)
            more = f" and {len(names) - 5} more" if len(names) > 5 else ""
            print(f"run cache: dropped the records of {', '.join(names[:5])}{more}: the cache changed during the run",
                  file=sys.stderr)
            return
        kept = {c: w for c, w in load_why(path).items() if c not in new}
        kept.update(why or {})
        for c, v in new.items():
            recs[c] = [v] + [r for r in recs.pop(c, []) if r[0] != v[0]][:KEEP - 1]  # newest first: the cap keeps them
        index = {}
        recs = {c: [[r[0], r[1], [index.setdefault(p, len(index)) for p in r[2]], *r[3:4]] for r in vs]
                for c, vs in list(recs.items())[-20000:]}
        kept = {c: w for c, w in kept.items() if c in recs}
        _write_json(path, {"gen": gen, "paths": list(index), "classes": recs, "why": kept})
    folder = os.path.dirname(path)
    with contextlib.suppress(OSError):
        for name in os.listdir(folder):
            q = os.path.join(folder, name)
            with contextlib.suppress(OSError):
                if q not in (path, path + ".lock") and time.time() - os.lstat(q).st_mtime > PRUNE_S:
                    if name.endswith(".runs") and os.path.isdir(q) and not os.path.islink(q):
                        shutil.rmtree(q, ignore_errors=True)  # T-4268: a repo's run records, unused as long
                    else:
                        os.unlink(q)


def _floor():
    try:
        return max(0.0, float(os.environ.get(CPU_FLOOR) or 20))
    except ValueError:
        return 20.0


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def _sn(x):
    """A CPU sample as (CPU s, the jobs it ran in, or None when not known): a split class's jobs each pay the
    interpreter and its fixtures again, so a sample compares only with samples of the same split."""
    if isinstance(x, (list, tuple)) and len(x) == 2:
        return float(x[0]), (int(x[1]) if isinstance(x[1], (int, float)) else None)
    return float(x), None


def cpu_over(hist, now, budgets=None):
    """T-3757: [(class, CPU s now, its usual or None, why)] for each class in `now` ({class: sample}) over its margin
    against `hist` ({class: [samples of its last green runs]}); a sample is CPU s or [CPU s, jobs], and the median of
    the samples with the same number of jobs is the usual. Over CPU_RATIO times its usual and the floor more; a class
    the baseline has never seen, once the baseline has any class, over 4.5 floors and its own CPU_BUDGET_S (`budgets`)
    (with an empty baseline nothing is new: the first green run seeds it); one seen only at another split is not
    compared. And when the compared classes took CPU_SUM_RATIO times their usual together and 6 floors more, each of
    them that grew by a quarter (all of them if none did): a rise spread too thin for any one class still fails."""
    budgets, floor, out, usual, spent = budgets or {}, _floor(), [], {}, {}
    for cid, x in sorted(now.items()):
        s, n = _sn(x)
        spent[cid] = s
        h = [_sn(y) for y in hist.get(cid) or () if isinstance(y, (int, float, list, tuple))]
        same = [hs for hs, hn in h if n is None or hn is None or hn == n]
        if same:
            usual[cid] = m = _median(same)
            if s > CPU_RATIO * m and s - m > floor:
                out.append((cid, s, m, f"{s / m:.1f}x its usual" if m else "its usual was 0"))
        elif not h and hist and s > max(4.5 * floor, budgets.get(cid) or 0):
            cap = max(4.5 * floor, budgets.get(cid) or 0)
            out.append((cid, s, None, f"a new class over {cap:g} CPU s"
                        + ("" if budgets.get(cid) else " (a CPU_BUDGET_S on the class declares more)")))
    tot, base = sum(spent[c] for c in usual), sum(usual.values())
    if base > 0 and tot > CPU_SUM_RATIO * base and tot - base > 6 * floor:
        named = {c for c, *_ in out}
        why = f"the run's {len(usual)} known classes took {tot:.0f} CPU s, {tot / base:.1f}x their usual {base:.0f}"
        grew = [c for c in usual if spent[c] > 1.25 * usual[c]] or list(usual)
        out += [(c, spent[c], usual[c], why) for c in sorted(grew) if c not in named]
    return out


def load_cpu(path):
    """T-3757: the baseline: {"classes": {class: [CPU s of its last CPU_KEEP green runs]}, "seen": {class: day},
    "days": {day: the suite's estimate}}; empty when missing or corrupt."""
    try:
        with open(path) as f:
            d = json.load(f)
        return {"classes": {c: [list(_sn(x)) for x in v][-CPU_KEEP:] for c, v in d.get("classes", {}).items()
                            if isinstance(v, list)},
                "seen": {c: str(v) for c, v in (d.get("seen") or {}).items()},
                "days": {k: float(v) for k, v in (d.get("days") or {}).items()}}
    except Exception:  # a corrupt baseline only costs a comparison
        return {"classes": {}, "seen": {}, "days": {}}


def record_cpu(path, now):
    """T-3757: a green run's {class: sample} added to the baseline under its lock, each class's last CPU_KEEP kept; a
    class unseen for CPU_UNSEEN_DAYS (renamed, deleted) dropped; today's estimate of the whole suite (every class's
    usual, summed) kept per day, the last 90."""
    today = time.strftime("%Y-%m-%d")
    old = time.strftime("%Y-%m-%d", time.localtime(time.time() - CPU_UNSEEN_DAYS * 86400))
    with _cache_lock(path):
        d = load_cpu(path)
        for cid, x in now.items():
            s, n = _sn(x)
            d["classes"][cid] = (d["classes"].get(cid, []) + [[round(s, 2), n]])[-CPU_KEEP:]
            d["seen"][cid] = today
        gone = [c for c in d["classes"] if d["seen"].get(c, today) < old]
        for cid in gone:
            d["classes"].pop(cid, None)
            d["seen"].pop(cid, None)
        d["days"][today] = round(sum(_median([s for s, _ in v]) for v in d["classes"].values() if v), 1)
        d["days"] = dict(sorted(d["days"].items())[-90:])
        _write_json(path, d)


def class_cpu(results, owner):
    """T-3757: {class: [CPU s, jobs]} of the classes whose every job ran green and reported its CPU."""
    got, bad = {}, set()
    for job, code, *_ in results:
        cid = owner(job)
        if code != 0 or job not in CPU:
            bad.add(cid)
        else:
            s, n = got.get(cid, (0.0, 0))
            got[cid] = (s + CPU[job], n + 1)
    return {cid: [round(s, 2), n] for cid, (s, n) in got.items() if cid not in bad}


def drop_cache(path):
    """T-3502: the cache found wrong: emptied at the next generation, so a run that read it before can't save it back."""
    with _cache_lock(path):
        _write_json(path, {"gen": load_cache(path)[0] + 1, "classes": {}})


EVENTS_KEEP = 500  # T-4274: the newest lines the events file keeps


def events_path(cache):
    return cache[:-len(".json")] + ".events.jsonl" if cache.endswith(".json") else cache + ".events.jsonl"


def note_event(cache, kind, cid):
    """T-4274: one line in the cache's events file (the class, the time, the load per CPU) for a flake (a class the
    cache has green failed once and passed again) or a drop (it failed twice: the cache was emptied). A run's stderr
    is rarely kept, so until this nothing said how often the cache was lost, or to which class. Never fails a run."""
    path = events_path(cache)
    line = json.dumps({"at": int(time.time()), "kind": kind, "class": cid, "load": round(float(load_per_cpu()), 2)})
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _cache_lock(path):
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "r+") as f:
                old = f.read().splitlines()
                if len(old) >= EVENTS_KEEP:
                    f.seek(0)
                    f.truncate()
                    f.write("".join(ln + "\n" for ln in old[-(EVENTS_KEEP - 1):]))
                f.write(line + "\n")


NO_JOIN = "SAMPO_NO_JOIN"  # T-4267: =1 never joins a class another run is running, nor marks the ones this run runs
POLL = 0.2  # T-4267: how often a waiter reads the owner's marker
JOIN_MIN_S, JOIN_TIMES = 120, 3  # T-4267: a marker older than max(JOIN_MIN_S, JOIN_TIMES x the class's known time) is dead
FLUSH_S = 2  # T-4267: a finished class waits at most this long for the one read of the tree that vouches for its green
SWEEP_S = 3600  # T-4267: a finished or dead marker this old is no run's any more (a run takes only one made while it ran)
DEFERRED = "run.py: deferred to a run beside this one (T-4267)\n"  # the result of a job another run's class took


def proc_ticks(pid):
    """T-4267: the process's start time in clock ticks (/proc/PID/stat field 22), so a recycled pid is told from the
    process that made a marker; '' where there is no /proc or no such process."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rpartition(")")[2].split()[19]
    except (OSError, IndexError, ValueError):
        return ""


def pid_alive(pid, ticks):
    """T-4267: whether pid is the process that recorded ticks (its start time); without ticks, whether pid exists."""
    if not isinstance(pid, int) or pid <= 0:  # os.kill(0, 0) is a whole process group
        return False
    if ticks:
        return proc_ticks(pid) == ticks
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # not ours to signal: it exists
        return True
    return True


def read_marker(path):
    """T-4267: a marker's dict, None when there is none, {} when it can't be read (a dead one: markers are made whole)."""
    try:
        with open(path) as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


class Joins:
    """T-4267 (part A of T-4266): two runs never run one test class on the same inputs at once. A class's inputs
    identity is the digest, on this tree, of the paths its newest cache record read (ident: class -> digest; a class
    never green has none and runs as before). The first run to start the class makes a marker for (class, digest) in
    the run-cache folder, shared by every worktree: its pid and start ticks, a time and a state (running, then green or
    red once every job of the class is in). A run that finds a marker held by a live owner defers the class (start()
    says no), runs its other classes, then wait()s: the owner's green is joined, not run; a red, dead or too-old owner,
    or no marker, means it runs the class itself.
    A join is a cache hit on a record made while this run ran, and held to the cache's rules (review): green is said
    only once the owner has read its tree again and what the class read is unchanged (flush(); a file edited mid-run
    makes it red: nobody else's), the marker carries the digest of what the class read (and the paths, when they are
    not its last record's) and the waiter takes it only when they read the same in its own tree, the whole class ran,
    and it finished after the waiter began (a plain full run takes no earlier result, so the cache is never asked);
    the owner's CPU sample comes with it (cpu), so a gate with a baseline compares a joined class too. A finished
    marker stays for the runs that were waiting; one a run left unfinished goes at its exit, and the next run sweeps
    what is older than SWEEP_S. Two waiters replacing one stale marker at once can both run the class: a duplicate,
    never a wrong result."""

    def __init__(self, cache, ident, value, owner, jobs, times, want=None, paths=None, read=None, fresh=None, t0=None):
        self.cache, self.ident, self.value, self.owner, self.times, self.want = cache, ident, value, owner, times, want
        # paths: class -> what its newest record read (ident digests them); read(jobs): the sorted paths those jobs
        # read in this run, None when nothing recorded it (without read: the record's); fresh(): the tree's values
        # now, None when it can't be read (without fresh: nothing to read again)
        self.paths, self.read, self.fresh = paths or {}, read, fresh
        self.t0 = time.time() if t0 is None else t0
        self.dir = os.path.join(os.path.dirname(cache), "running")
        self.tag = os.path.basename(cache).partition(".")[0]
        self.jobs = {}
        for j in jobs:
            self.jobs.setdefault(owner(j), []).append(j)
        self.state, self.got, self.made = {}, {}, {}  # class -> mine/theirs/free; job -> result; class -> its marker
        self.pend, self.cpu, self.flusher = {}, {}, None  # class -> its green not yet vouched for; joined class -> CPU
        self.lock, self.flock = threading.Lock(), threading.Lock()
        self.me = {"pid": os.getpid(), "ticks": proc_ticks(os.getpid())}
        self._sweep()
        atexit.register(self.close)

    def path(self, c):
        return os.path.join(self.dir, f"{self.tag}.{hashlib.sha1(c.encode('utf-8', 'surrogateescape')).hexdigest()[:12]}."
                                      f"{self.ident[c][:24]}")

    def limit(self, c):
        return max(JOIN_MIN_S, JOIN_TIMES * self.times.get(c, 0))

    def _sweep(self):
        """Markers and claims' temp files older than SWEEP_S go, but a class a live run is still running."""
        with contextlib.suppress(OSError):
            for name in os.listdir(self.dir):
                q = os.path.join(self.dir, name)
                with contextlib.suppress(OSError):
                    if time.time() - os.lstat(q).st_mtime > SWEEP_S:
                        m = None if name.endswith(".new") else read_marker(q)
                        if not (m and m.get("state") == "running" and pid_alive(m.get("pid"), m.get("ticks"))):
                            os.unlink(q)

    def _live(self, m, c):
        """Whether the marker m is a run to wait for: running, its owner alive, younger than the class's limit."""
        at = m.get("at") if isinstance(m.get("at"), (int, float)) else 0
        return m.get("state") == "running" and pid_alive(m.get("pid"), m.get("ticks")) and time.time() - at < self.limit(c)

    def _good(self, m, c):
        """The tests of a green marker this run may take, else None: finished since this run began, the whole class,
        and what the owner's class read reads the same in this tree."""
        n, read, done = m.get("n"), m.get("paths", self.paths.get(c, [])), m.get("done")
        if m.get("state") != "green" or not isinstance(done, (int, float)) or done < self.t0:
            return None
        if not isinstance(n, int) or isinstance(n, bool) or not self._fit(c, n):
            return None
        if not isinstance(read, list) or not all(isinstance(p, str) for p in read) or not isinstance(m.get("sum"), str):
            return None
        return n if digest(read, self.value) == m["sum"] else None

    def _claim(self, c):
        """'mine' (a marker made), 'theirs' (another's: running, or a green to take) or 'free' (no marker could be
        made: run anyway)."""
        path = self.path(c)
        mine = {**self.me, "at": time.time(), "state": "running"}
        temp = f"{path}.{os.getpid()}.new"  # the process's own: a name two shared let one publish the other's pid
        for _ in range(3):
            m = read_marker(path)
            if m is not None:
                if self._live(m, c) or self._good(m, c) is not None:
                    return "theirs"
                if read_marker(path) == m:  # the same stale one still: gone before the new one is made
                    with contextlib.suppress(OSError):
                        os.unlink(path)
            try:
                os.makedirs(self.dir, mode=0o700, exist_ok=True)
                _write_json(temp, mine)
                try:
                    os.link(temp, path)  # whole content or none, and never over another's marker
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(temp)
            except FileExistsError:
                continue
            except OSError:
                return "free"
            self.made[c] = mine
            return "mine"
        return "free"

    def start(self, job):
        """Whether to run this job: False when its class is another run's, which is deferred to wait()."""
        c = self.owner(job)
        with self.lock:
            if c not in self.state:
                self.state[c] = self._claim(c) if c in self.ident and c in self.jobs else "free"
            return self.state[c] != "theirs"

    def deferred(self):
        with self.lock:
            return {c for c, s in self.state.items() if s == "theirs"}

    def _say(self, c, state, **more):
        self.made[c] = {**self.made[c], "state": state, "done": time.time(), **more}
        _write_json(self.path(c), self.made[c])

    def done(self, res):
        """A job's result: once every job of a class this run owns is in, its marker says red at once (a failure, a
        skip, or nothing recorded what it read), or green after the next flush()."""
        c = self.owner(res[0])
        with self.lock:
            self.got[res[0]] = res
            if self.state.get(c) != "mine" or c not in self.made or not all(j in self.got for j in self.jobs[c]):
                return
            rs = [self.got[j] for j in self.jobs[c]]
        green = all(r[1] == 0 and not re.search(r"\bskipped=\d", r[2]) for r in rs)
        read = None if not green else list(self.paths.get(c, [])) if self.read is None else self.read(self.jobs[c])
        with self.lock:
            if read is None:
                self._say(c, "red")
                return
            cpu = class_cpu(rs, self.owner).get(c)
            self.pend[c] = {"n": sum(r[3] for r in rs), "read": read, **({"cpu": cpu} if cpu else {})}
            later = self.fresh is not None and self.flusher is None
            if later:
                self.flusher = threading.Thread(target=self._later, daemon=True)
                self.flusher.start()
        if self.fresh is None:
            self.flush()

    def _later(self):
        time.sleep(FLUSH_S)
        with self.lock:
            self.flusher = None  # a class finished from here on starts the next
        self.flush()

    def flush(self):
        """Say green for the finished classes waiting for it: one read of the tree now vouches for all of them. A
        class one of whose paths reads otherwise now than at the start (it ran the old code or the new), or a tree
        that can't be read, is red: this run's result, nobody else's."""
        with self.flock:
            with self.lock:
                pend, self.pend = self.pend, {}
            if not pend:
                return
            now = self.fresh() if self.fresh else self.value
            for c, d in pend.items():
                total = digest(d["read"], self.value)
                same = now is not None and (now is self.value or digest(d["read"], now) == total)
                with self.lock:
                    if c not in self.made:  # closed meanwhile
                        continue
                    if same:
                        self._say(c, "green", n=d["n"], sum=total, **({"cpu": d["cpu"]} if "cpu" in d else {}),
                                  **({} if d["read"] == self.paths.get(c, []) else {"paths": d["read"]}))
                    else:
                        self._say(c, "red")

    def _fit(self, c, n):
        return self.want is None or self.want.get(c) == n  # a run that ran part of the class is not the class's result

    def take(self, classes):
        """These deferred classes are this run's to run after all (a joined one over its CPU margin), unmarked."""
        with self.lock:
            for c in classes:
                self.state[c] = "free"

    def wait(self, end):
        """Wait (at most until the epoch end) for every deferred class: {class: tests} of those joined, the owner's
        green result taken (its CPU sample in self.cpu). A class not joined (a red, dead or old owner, no marker, a
        green that reads otherwise here, the deadline) is this run's to run: start() now says yes."""
        pend, joined = sorted(self.deferred()), {}
        if pend:
            sys.stderr.write(f"run.py: joining {len(pend)} class(es) a run beside this one is running: "
                             f"{', '.join(pend[:3])}{', ...' if len(pend) > 3 else ''}\n")
            sys.stderr.flush()
        while pend:
            late = time.time() >= end - 1 or bool(ENDED)  # T-4399: ended from outside: nothing more is waited for
            for c in list(pend):
                m = read_marker(self.path(c))
                if m and self._live(m, c) and not late:
                    continue  # still running
                n = self._good(m, c) if m else None
                if n is not None:
                    joined[c] = n
                    if m.get("cpu") is not None:
                        self.cpu[c] = m["cpu"]
                else:  # runs it: marks it if it can, unmarked at the deadline
                    with self.lock:
                        self.state[c] = "free" if late else self._claim(c)
                        if self.state[c] == "theirs":  # another waiter took it meanwhile, or its green came in
                            continue
                pend.remove(c)
            if pend:
                time.sleep(POLL)
        return joined

    def close(self):
        """Say what is still to say, then remove the markers of the classes this process never finished (idempotent;
        a marker another run replaced is not touched). A finished one stays for the runs waiting on it."""
        self.flush()
        with self.lock:
            for c, mine in list(self.made.items()):
                m = read_marker(self.path(c)) if mine.get("state") == "running" else None
                if m is not None and m.get("state") == "running" and m.get("pid") == mine["pid"] \
                        and m.get("ticks") == mine["ticks"]:
                    with contextlib.suppress(OSError):
                        os.unlink(self.path(c))
                del self.made[c]


def _git(cwd, *args, data=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # a hook's GIT_DIR would hash another tree
    return subprocess.run(["git", "-C", cwd, *args], input=data, capture_output=True, text=True, errors="surrogateescape", env=env, timeout=60,
                          check=True).stdout


def tree():
    """T-3502: (repo root, {path: "mode blob"}, the repo's shared git folder) of the files as they are on disk: the
    index's, a modified or untracked file hashed again; GIT_STATE: HEAD and the tags, hashed. None outside git. T-3593: the cache is the git folder's,
    so every worktree of the repo (a lane's, a train's scratch tree) reads and adds to one. T-3691: "\\0HEAD" HEAD's
    commit, "\\0ref:HEAD" its branch's full name, "\\0ref:refs/remotes/X/HEAD" a remote's default branch (both with
    the tags' and remote branches' names)."""
    try:
        repo = _git(HERE, "rev-parse", "--show-toplevel").strip()
        common = os.path.realpath(os.path.join(repo, _git(repo, "rev-parse", "--git-common-dir").strip()))
        blobs = {}
        for ln in _git(repo, "ls-files", "-s", "-z").split("\0"):
            if ln:
                meta, path = ln.split("\t", 1)
                blobs[path] = " ".join(meta.split()[:2])  # mode and blob: a chmod -x is a change (review)
        dirty = _git(repo, "ls-files", "-z", "-m", "-o", "--exclude-standard").split("\0")
        dirty = list(dict.fromkeys(f for f in dirty if f))
        for f in dirty:
            blobs.pop(f, None)  # deleted ones stay out
        live = [f for f in dirty if os.path.isfile(os.path.join(repo, f))]
        if any("\n" in f for f in live):
            return None  # hash-object reads a path a line
        if live:
            hashed = _git(repo, "hash-object", "--stdin-paths", data="\n".join(live) + "\n").split()
            blobs.update((f, f"{'100755' if os.stat(os.path.join(repo, f)).st_mode & 0o100 else '100644'} {b}")
                         for f, b in zip(live, hashed))
        try:  # what git itself says (log, describe, rev-parse) to a class that ran git on the repo: HEAD and the tags,
            head = _git(repo, "rev-parse", "HEAD", "--symbolic-full-name", "HEAD")  # not every ref: a lane's commit
        except subprocess.CalledProcessError:  # no commit yet                       moves one, review
            head = ""
        rows = [ln.split(" ", 2) for ln in _git(repo, "for-each-ref", "--format=%(symref) %(objectname) %(refname)",
                                                "refs/tags", "refs/remotes").splitlines() if ln]
        tags = "".join(f"{o} {r}\n" for _, o, r in rows if r.startswith("refs/tags/"))
        blobs[GIT_STATE] = hashlib.sha1((head + tags).encode()).hexdigest()
        # T-3691: a short ref name's ambiguity (abbrev-ref, --short) rides on these names: the mark (not refs/heads,
        # which every lane adds to)
        mark = hashlib.sha1("".join(r + "\n" for _, _, r in rows).encode()).hexdigest()[:12]
        h = head.split("\n")
        blobs["\0HEAD"] = h[0] if head else ""
        blobs["\0ref:HEAD"] = f"{h[1]} {mark}" if head else os.urandom(8).hex()  # unborn: abbrev-ref fails, rerun
        blobs.update(("\0ref:" + r, f"{s} {mark}") for s, _, r in rows
                     if r.startswith("refs/remotes/") and r.endswith("/HEAD"))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return repo, blobs, common


def _roots(repo):
    """The repo's folder as git names it, resolved, and as this run.py reaches it, each ending in a separator."""
    return sorted({os.path.join(r, "") for r in (repo, os.path.realpath(repo), os.path.dirname(os.path.dirname(HERE)))})


def cover_python():
    """T-3593: a venv whose python loads runcover.py (through a .pth) in every Python started with it, so each process
    records the repo files it reads; that python, or None where it can't be made (not POSIX, no runcover.py beside
    run.py, a python that doesn't start). One per interpreter and runcover.py under the cache folder, outside /tmp,
    which a job sandbox (bwrap --tmpfs /tmp) hides; made aside and renamed in, so a run sees it whole or not at all;
    one no run used for PRUNE_S goes."""
    src = os.path.join(HERE, "runcover.py")
    if os.name != "posix" or not os.path.isfile(src):
        return None
    real = os.path.realpath(sys.executable)
    own = [p for p in site.getsitepackages() + ([site.getusersitepackages()] if site.ENABLE_USER_SITE else [])
           if os.path.isdir(p)]  # run.py's own packages (a venv's) stay importable
    with open(src, "rb") as f:
        key = hashlib.sha1(f"{real}\0{sys.version}\0{own}\0python python3\0".encode() + f.read()).hexdigest()[:16]
    top = os.path.join(_cache_base(), "sampo", "run-py")
    d = os.path.join(top, key)
    py = os.path.join(d, "bin", "python3")
    try:
        if not os.path.exists(py):
            os.makedirs(top, exist_ok=True)
            new = tempfile.mkdtemp(dir=top, prefix=".new-")
            try:
                lib = os.path.join(new, os.path.relpath(site.getsitepackages([d])[0], d))
                os.makedirs(lib)
                os.mkdir(os.path.join(new, "bin"))
                for name in ("python", "python3"):  # first on a class's PATH: `python3 x.py`, fm's #! line record too
                    os.symlink(real, os.path.join(new, "bin", name))
                with open(os.path.join(new, "pyvenv.cfg"), "w") as f:
                    f.write(f"home = {os.path.dirname(real)}\ninclude-system-site-packages = true\n")
                shutil.copy(src, lib)
                with open(os.path.join(lib, "runcover.pth"), "w") as f:
                    f.write("".join(p + "\n" for p in own) + f"import runcover; runcover.setup({py!r})\n")
                subprocess.run([os.path.join(new, "bin", "python3"), "-c", "import runcover, sys; sys.exit(not runcover.PY)"],
                               check=True, timeout=120, capture_output=True)
                with contextlib.suppress(OSError):  # a run beside this one's won: the same venv
                    os.rename(new, d)
            finally:
                shutil.rmtree(new, ignore_errors=True)  # gone once renamed
        os.utime(d)  # in use: the pruning below keeps it
        for name in os.listdir(top):
            if name != key and time.time() - os.lstat(os.path.join(top, name)).st_mtime > PRUNE_S:
                shutil.rmtree(os.path.join(top, name), ignore_errors=True)
    except (OSError, IndexError, subprocess.SubprocessError):
        return None
    return py if os.path.exists(py) else None


def _in_temp(path, roots):
    """T-5484: whether a path is a scratch repo a test built in its own temp folder: under a fmtest-<pid>- folder (the
    class child's TMPDIR, made directly in the temp folder). Never when the temp folder is '/' or holds a repo root:
    then no folder of it is one the test made, and a HEAD there may be the tree's."""
    tmp = os.path.join(os.path.realpath(tempfile.gettempdir()), "")
    if tmp == os.sep or any(r.startswith(tmp) for r in roots):
        return False
    p = os.path.join(os.path.realpath(path), "")
    if not p.startswith(tmp):
        return False

    def fmtest(name):  # the name tmp.mkdtemp gives a class child's folder: TMP_PREFIX, its run.py's pid, a dash
        return name.startswith(TMP_PREFIX) and name[len(TMP_PREFIX):].split("-", 1)[0].isdigit()
    # the class child's own folder is the temp root; the run.py parent's view has it as a folder of the temp root
    return fmtest(os.path.basename(tmp.rstrip(os.sep))) or fmtest(p[len(tmp):].split(os.sep, 1)[0])


def covered(text, repo, blobs):
    """T-3593: the paths a class's recorded lines name, relative to the repo: its files (a module's source for its
    .pyc), its listed folders ('x/', './' the root), '*' for the whole tree. A file on disk that git doesn't see
    (ignored, or under .git/) and Sampo's task state are left out; a missing one stays, so creating it reruns.
    Accepted gaps (the 1-in-10 rerun is their backstop): the import system's own folder listings, so a new module
    shadowing one later on sys.path goes unseen, and where an in-repo symlink points.
    T-3603: a module it ran is 'x.py::' (the file less its functions' bodies), a function it ran 'x.py::QUALNAME' and
    its module's 'x.py::'; a lambda's, a generator expression's or an annotation's outside any function counts as the
    module's. A file it read whole stands for its parts; a .py it only stat'ed (T-5586) is its 'x.py::' too. Accepted gaps: output that depends on where in the file a
    function is (a line number logged, not a traceback, which reads the file) and code read without being run
    (f.__code__)."""
    roots, out = _roots(repo), set()
    for ln in text.splitlines():
        kind, _, p = ln.partition(" ")
        if kind in ("*", "g"):  # a child it couldn't follow; "g": that child was git on the repo
            out.add("*" if kind == "*" else GIT_STATE)
            continue
        if kind in ("r", "h"):  # T-3691: git on a repo read a ref or a path at HEAD: this tree's, else unbounded
            top, _, name = p.partition("\0")
            if os.path.join(top, "") in roots:
                out.add(("\0ref:" + name) if kind == "r" else "\0HEAD" + (":" + name if name else ""))
            elif not _in_temp(top, roots):  # T-5484: a scratch repo the test built in the temp folder reads nothing of the tree
                out.update(("*", GIT_STATE))
            continue
        qual = None
        if kind == "c":
            p, _, qual = p.rpartition(" ")
            qual = "" if qual.rsplit(".", 1)[-1].startswith("<") else qual  # no def names it: the module's code
        root = next((r for r in roots if (p + os.sep).startswith(r)), None)
        if root is None or kind not in ("f", "d", "s", "m", "c"):
            continue
        rel = p[len(root):].replace(os.sep, "/")
        if kind == "s":  # stat, exists, access: a file's content and mode, else whether it is there (review)
            if rel and not rel.startswith(STATE):
                # T-5586: a stat of a Python file reads its mode and size, not a function's body (recall's verb-docs stamp
                # stats every module of the package): its module code and mode, so a body edit reruns only who ran it
                out.add(rel + "::" if rel in blobs and rel.endswith(".py") else rel if rel in blobs else "?" + rel)
            continue
        if kind == "d":
            if not (rel + "/").startswith(STATE):
                out.add(rel.rstrip("/") + "/" if rel else "./")
            continue
        if "__pycache__/" in rel and rel.endswith(".pyc"):
            with contextlib.suppress(ValueError):
                rel = importlib.util.source_from_cache(rel)
        if rel and not rel.startswith(STATE) and (rel in blobs or not os.path.lexists(os.path.join(repo, rel))):
            out.update([rel] if kind == "f" else [rel + "::", rel + "::" + qual] if qual else [rel + "::"])
    return {p for p in out if p in blobs or "::" not in p or p.rpartition("::")[0] not in out}  # a part of a whole one


def shared(repo, blobs):
    """T-3593: what every class depends on: run.py and the helpers beside it (helpers.py, forkcli.py, runcover.py)."""
    rel = os.path.relpath(os.path.realpath(HERE), os.path.realpath(repo)).replace(os.sep, "/")
    return {f for f in blobs if f.startswith(rel + "/") and f.endswith(".py") and "/" not in f[len(rel) + 1:]
            and not os.path.basename(f).startswith("test") and f[len(rel) + 1:] != "cachecost.py"}  # the report, not a run


def _yields(body):
    """Whether a function's own code (not a function or class inside it, whose defaults and bases are its) yields."""
    todo = list(body)
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(c for name, v in ast.iter_fields(n) if name != "body"
                        for c in (v if isinstance(v, list) else [v]) if isinstance(c, ast.AST))
        else:
            todo.extend(ast.iter_child_nodes(n))
    return False


_BY_NAME = ("globals", "dir", "vars", "locals")  # T-5499: what reads a module's names without writing them
_BY_UNITTEST = ("setUpModule", "tearDownModule", "load_tests")  # T-5499: unittest looks these up on a module by name


def _notes(fn):
    """T-5499: a def's annotations (its arguments' and its return), which run at import before Python 3.14."""
    a = fn.args
    return [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if x and x.annotation] \
        + ([fn.returns] if fn.returns else [])


def _own(tree):
    """T-5499: the module-level defs that are only a name and a signature: ({id(def)}), the rest of the module not
    able to tell which def it is. A def with no decorator, constant defaults (a number, string, None, True, a minus
    sign before one), constant annotations (any, with `from __future__ import annotations`), a name bound nowhere
    else in the module (an import, an assignment, a second def, a global statement, a builtin, a dunder like
    __getattr__, setUpModule, tearDownModule or load_tests, which unittest finds by name) and no use at import time
    (a default, a decorator, a class body, module code: a use inside any function, or in the body of
    `if __name__ == "__main__":`, is a call that runs the def, so its caller's record holds it; its else branch and
    other tests run at import). A module that reads its own names (globals(), dir(), vars(), locals()) or imports
    `*` has none."""
    binds, loads = [], set()
    todo = [(n, True, False) for n in tree.body]  # a node, whether it binds a module name, whether its loads count
    while todo:
        n, bind, quiet = todo.pop()
        kids = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind and binds.append(n.name)
            kids += n.decorator_list
            if isinstance(n, ast.ClassDef):
                kids += n.bases + [k.value for k in n.keywords]
                todo.extend((c, False, quiet) for c in n.body)  # a name a class binds is the class's
            else:
                kids += n.args.defaults + [d for d in n.args.kw_defaults if d] + _notes(n)
        elif isinstance(n, ast.Lambda):
            kids += n.args.defaults + [d for d in n.args.kw_defaults if d]
        else:
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Load):
                    quiet or loads.add(n.id)
                elif bind:
                    binds.append(n.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                if any(a.name == "*" for a in n.names):
                    return set()
                bind and binds.extend((a.asname or a.name).split(".")[0] for a in n.names)
            elif isinstance(n, ast.ExceptHandler):
                bind and n.name and binds.append(n.name)
            elif isinstance(n, (ast.MatchAs, ast.MatchStar)):
                bind and n.name and binds.append(n.name)
            elif isinstance(n, ast.MatchMapping):
                bind and n.rest and binds.append(n.rest)
            if (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Name)
                    and n.test.left.id == "__name__" and [type(o) for o in n.test.ops] == [ast.Eq]
                    and [getattr(c, "value", 0) for c in n.test.comparators] == ["__main__"]):
                todo.extend((c, bind, True) for c in n.body)  # `if __name__ == "__main__":` runs as a script: only its body
                kids += [n.test] + n.orelse
            else:
                kids += ast.iter_child_nodes(n)
        todo.extend((k, bind, quiet) for k in kids)
    of_other = set()  # dir(x), vars(x): not the module's (ast.walk gives a call before its func)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("dir", "vars") and (n.args or n.keywords):
            of_other.add(id(n.func))
        elif isinstance(n, ast.Name) and n.id in _BY_NAME and id(n) not in of_other:
            return set()
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            binds.extend(n.names)
        elif isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
            binds.append(n.target.id)
    const = lambda d: isinstance(d, ast.Constant) or (isinstance(d, ast.UnaryOp) and isinstance(d.operand, ast.Constant))  # noqa: E731
    lazy = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
               for n in tree.body)  # no annotation runs at import
    return {id(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.decorator_list
            and all(const(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d])
            and (lazy or all(isinstance(a, ast.Constant) for a in _notes(n)))
            and binds.count(n.name) == 1 and n.name not in loads and not hasattr(builtins, n.name)
            and n.name not in _BY_UNITTEST and not (n.name.startswith("__") and n.name.endswith("__"))}


def units(data):
    """T-3603: a Python source's parts, ({function qualname: its hash}, the rest's hash), None when it doesn't
    parse. A function's body with its docstring (no code here reads a __doc__ it didn't call) and the functions and
    classes in it ("f.<locals>.g" is f's); the rest is the file with each body cut to whether it yields (a generator
    never started ran none of its body: turning one into a function reruns every class that ran its module). Neither
    holds line numbers or comments. A qualname defined twice (an if/else) is one part of both bodies.
    T-5499: a module-level def that is only its name and signature (_own()) is no part of the rest: its part holds its
    signature too, so adding, removing or re-signing one moves its own part and no class that ran the module reruns
    for it; the rest keeps a note of each such def that yields. A method stays in its class's part of the rest.
    Accepted gaps (the 1-in-10 rerun is their backstop): a class that imported a name without calling it isn't rerun
    when it goes, nor one that lists it in `__all__` and is star-imported by another module, nor one that
    mock.patch("mod.g")es a g it never ran."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape's SyntaxWarning is the module's business
            tree = ast.parse(data)
    except Exception:  # not Python: a syntax error, a null byte, too deep
        return None
    funcs, own = {}, _own(tree)

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                b, name = node.body, prefix + node.name
                head = (f"{type(node).__name__} {ast.dump(node.args)} {ast.dump(node.returns) if node.returns else ''} "
                        f"{[ast.dump(t) for t in getattr(node, 'type_params', ())]}" if id(node) in own else "")
                funcs[name] = funcs.get(name, "") + "\0" + head + "\n".join(ast.dump(s) for s in b)
                node.body = [ast.Expr(ast.Constant(_yields(b)))]
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")
            else:  # if, for, while, with, try, match: a def in them is the module's (or class's)
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, None) or [], prefix)
                for sub in (getattr(node, "handlers", None) or []) + (getattr(node, "cases", None) or []):
                    walk(sub.body, prefix)
    walk(tree.body, "")
    tree.body = [ast.Expr(ast.Constant("yields " + n.name)) if id(n) in own and n.body[0].value.value else n
                 for n in tree.body if id(n) not in own or n.body[0].value.value]  # a stub: whether the def yields
    h = lambda s: hashlib.sha1(s.encode("utf-8", "surrogateescape")).hexdigest()  # noqa: E731
    return {q: h(s) for q, s in funcs.items()}, h(ast.dump(tree))


_UNITS = {}  # T-3603: blob -> units() of its bytes, for every values() of this run


def _units(repo, rel, blob):
    """units() of a tree's file ("mode blob"): its bytes from disk when they are the blob's, else from git; None for a
    symlink or a submodule, or bytes git can't give (a dirty file a filter changed)."""
    mode, _, sha = blob.partition(" ")
    if mode not in ("100644", "100755"):
        return None
    if sha not in _UNITS:
        data = None
        with contextlib.suppress(OSError):
            with open(os.path.join(repo, rel), "rb") as f:
                data = f.read()
        if data is None or hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != sha:
            data = None
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                data = subprocess.run(["git", "-C", repo, "cat-file", "blob", sha], capture_output=True, timeout=60,
                                      check=True, env={k: v for k, v in os.environ.items()
                                                       if not k.startswith("GIT_")}).stdout
        _UNITS[sha] = None if data is None else units(data)
    return _UNITS[sha]


_HEADS = {}  # T-3691: commit -> {path: "mode type object"} of its tree, for every values() of this run


def _at_head(repo, commit, path):
    """T-3691: what `git show HEAD:path` prints rides on: path's entry in the commit's tree ('' when absent, '-' with
    no commit); a git that fails gives a value no record has (rerun)."""
    if not commit:
        return "-"
    if commit not in _HEADS:
        try:
            rows = _git(repo, "ls-tree", "-r", "-t", "-z", "--full-tree", commit).split("\0")
            _HEADS[commit] = {name: meta for meta, _, name in (ln.partition("\t") for ln in rows if ln)}
        except (OSError, subprocess.SubprocessError):
            return os.urandom(8).hex()
    return _HEADS[commit].get(path, "")


NAMED_BLIND = ("* browser ", "* page ")  # T-5484: a browser's launch, and the page a driver sent it (sampo.page)


def run_paths(base, texts, repo, blobs, reads):
    """T-5484: the paths a class's record holds: base, and what each run's recorded lines name (covered()). The '*'
    (the whole tree) gives way to the class's RUN_READS only when every '*' its runs wrote is a named browser's or
    page's line (NAMED_BLIND): then the declared files, each tracked, a declared folder ('x/') with tracked files under
    it. Anything else, an undeclared or untracked entry, no declaration, keeps the whole tree."""
    out, named, pages = set(base), False, []
    for text in texts:
        lines = text.splitlines()
        named = named or any(ln.startswith(NAMED_BLIND) for ln in lines)
        pages += [ln[len("* page "):] for ln in lines if ln.startswith("* page ")]
        out.update(covered("\n".join(ln for ln in lines if not ln.startswith(NAMED_BLIND)), repo, blobs))
    if not named or "*" in out:
        return out
    if not reads:
        return out | {"*"}
    got = set()
    for e in reads:
        if not isinstance(e, str) or not e or e.startswith(("/", STATE)) or ".." in e.split("/"):
            return out | {"*"}
        if e.endswith("/"):
            files = {f for f in blobs if f.startswith(e) and f[:1] != "\0"}
            if not files:
                return out | {"*"}
            got |= {e} | files
        elif e in blobs:
            got.add(e)
        else:
            return out | {"*"}
    roots = _roots(repo)
    for url in pages:  # T-5484: a page the declaration omits loads a file of the tree the declared reads don't name
        rel = _page_rel(url, roots)
        if rel is not None and rel not in got:
            return out | {"*"}
    return out | got


def _page_rel(url, roots):
    """T-5484: the repo-relative path of the file a page's URL loads (file: only, resolved as the browser does), None
    when the URL names no file under a root (another scheme, or outside the repo); '' for the root itself."""
    from urllib.parse import unquote, urlsplit
    if url.strip()[:5].lower() != "file:":
        return None
    path = unquote(urlsplit(url.strip()).path)
    if not path.startswith("/"):
        return ""  # a relative file: URL resolves in the browser's folder, which no entry names
    full = os.path.join(os.path.realpath(path), "")
    root = next((r for r in roots if full.startswith(r)), None)
    if root is None:
        return None
    return full[len(root):].rstrip(os.sep).replace(os.sep, "/")


def values(repo, blobs):
    """T-3593: what a recorded path is worth in a tree: a file its mode and blob ('' when gone), a folder ('x/') the
    names in it, '?x' whether x is a file, a folder or absent, '*' the whole tree's files (Sampo's task state left
    out), GIT_STATE HEAD and the tags. T-3603: 'x.py::QUALNAME' its function's body, 'x.py::' its mode and the rest
    of the file (units()); either the file's blob when it doesn't parse or no def has that name (code compiled from
    a string with the file's name), an annotation function's the rest's."""
    memo, kids = {}, {}

    def value(p):
        if p not in memo:
            if p[:1] == "\0":  # T-3691: git's own state (tree()); a path at HEAD its entry in HEAD's tree
                memo[p] = _at_head(repo, blobs.get("\0HEAD", ""), p[6:]) if p.startswith("\0HEAD:") else blobs.get(p, "")
            elif p == "*":
                memo[p] = hashlib.sha1("".join(f"{f}\0{b}\0" for f, b in sorted(blobs.items())
                                               if not f.startswith(STATE) and f[:1] != "\0").encode("utf-8", "surrogateescape")).hexdigest()
            elif p.endswith("/") or p.startswith("?"):
                if not kids:
                    for f in blobs:
                        parts = f.split("/") if f[:1] != "\0" else []
                        for i, name in enumerate(parts):
                            kids.setdefault("/".join(parts[:i]) + "/" if i else "./", set()).add(name)
                q = p[1:]
                memo[p] = ("/".join(sorted(kids.get(p, ()))) if p.endswith("/") else
                           "file" if q in blobs else "dir" if q + "/" in kids else "")
            elif p in blobs or "::" not in p:
                memo[p] = blobs.get(p, "")
            else:
                rel, _, q = p.rpartition("::")
                b = blobs.get(rel, "")
                u = _units(repo, rel, b) if b else None
                memo[p] = (b if u is None else u[0][q] if q in u[0] else
                           f"{b.partition(' ')[0]} {u[1]}" if not q or q.rsplit(".", 1)[-1] == "__annotate__" else b)
        return memo[p]
    return value


def digest(paths, value):
    """T-3593: one hash of the paths' values (sorted paths), Python's version, --sandbox and the local-models switch."""
    h = hashlib.sha1(f"{sys.version}\0{bool(SANDBOX)}\0{os.environ.get('FOREMAN_NO_LOCAL_MODELS', '1')}".encode())
    for p in paths:
        h.update(f"{p}\0{value(p)}\0".encode("utf-8", "surrogateescape"))  # T-3651: a name with a stray byte
    return h.hexdigest()


def work_time(secs):
    """A split class's work: its tests' seconds less each one's start-up, so it stays split only while it is slow."""
    return round(max(0.0, sum(secs) - START_S * len(secs)), 2)


def plan(todo, parts, whole, times):
    """T-3166: the jobs for these classes, longest first. A class that may be split (not in whole) is, once its work
    took SPLIT_S or more last time (or, never timed, when SEEDED names it): its tests become jobs."""
    jobs = []
    for cid in todo:
        ids = parts.get(cid, [])
        split = cid not in whole and len(ids) > 1 and (times[cid] >= SPLIT_S if cid in times else cid.rsplit(".", 1)[-1] in SEEDED)
        jobs += ids if split else [cid]

    return sorted(jobs, key=lambda j: -job_seconds(j, times, parts))  # stable: ties keep the biggest-class-first order


def job_seconds(j, times, parts):
    """Seconds a job (a class, or one test of a split class) is expected to take: its last time, else a guess."""
    if j in times:
        return times[j]
    if j in parts:  # a whole class never timed: half a second a test
        return 0.5 * len(parts[j])
    c = j.rsplit(".", 1)[0]  # a test never timed: its class's average
    return times[c] / len(parts[c]) if c in times and c in parts else 0.5


def classes(patterns, k, hit=None, names=(), ids=None, parts=None, whole=None):
    """Every test class id (module.Class) discover would run, biggest first so the long ones start early; hit (a set)
    gets each -k pattern or name that matched a test. T-1994: with names and -k both, a test matches one of each.
    ids (a list) gets every test id walked, before any filter. T-3166: parts (a dict) gets each class's matching
    test ids, and whole (a set) the classes that keeps_whole."""
    sys.path.insert(0, HERE)
    counts, skip, hit = {}, skipped(), set() if hit is None else hit

    def walk(suite):
        for t in suite:
            if isinstance(t, unittest.TestSuite):
                walk(t)
                continue
            if skip and (t.id().rsplit(".", 1)[-1] if type(t).__name__ == "_FailedTest"  # a module that won't import
                           else type(t).__module__) in skip:
                continue
            if ids is not None:
                ids.append(t.id())
            if (not k or any(x in t.id() for x in k)) and (not names or any(x in t.id() for x in names)):
                hit.update(x for x in (*k, *names) if x in t.id())
                cid = f"{type(t).__module__}.{type(t).__name__}"
                if isinstance(getattr(type(t), "CPU_BUDGET_S", None), (int, float)):  # T-3757
                    BUDGETS[cid] = float(type(t).CPU_BUDGET_S)
                reads = getattr(sys.modules.get(type(t).__module__), "RUN_READS", None)  # T-5484: module-level
                if isinstance(reads, (list, tuple)):
                    READS[cid] = list(reads)
                counts[cid] = counts.get(cid, 0) + 1
                if parts is not None and t.id() not in parts.setdefault(cid, []):  # an imported TestCase is walked twice
                    parts[cid].append(t.id())
                if whole is not None and keeps_whole(t):
                    whole.add(cid)
    for pattern in dict.fromkeys(patterns):
        walk(unittest.TestLoader().discover(HERE, pattern=pattern))  # not the shared one: unittest -k filters it
    return sorted(counts, key=lambda c: -counts[c])


def annotations(cid, err):
    """T-1248: one GitHub ::error line per failing test (its title, the line that failed and the exception); output
    with no FAIL:/ERROR: blocks (an import error) keeps its last four lines."""
    out = []
    for name, block in re.findall(r"(?m)^(?:FAIL|ERROR): (\w+) \([^)]*\)\n-+\n(.*?)(?=\n=+\n|\n-+\nRan |\Z)", err, re.S):
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        code = [x for x in lines if not x.startswith(("Traceback", "File ")) and lines.index(x) < len(lines) - 1]
        body = ([code[-1]] if code else []) + lines[-1:]
        out.append(f"::error title={cid}.{name}::" + "%0A".join(x[:250] for x in body).replace("::", ": :"))
    if not out:  # T-1911: lines cut to 250, and a class that wrote nothing says so (an empty message isn't shown)
        tail = [x.strip()[:120] for x in err.splitlines() if x.strip() and not set(x.strip()) <= set("-=~^")][-4:]
        out.append(f"::error title={cid}::" + ("%0A".join(tail) or "no output").replace("::", ": :"))
    return out


NOT_STARTED = "run.py: not started: the run reached its --deadline (T-1809)\n"
TMP_PREFIX = "fmtest-"  # not fm-: fm tidy reads ~/.claude/projects/-tmp-fm-* as its own claude -p runs' folders


# T-2034: a class dies with run.py, even a SIGKILLed one (a Bash timeout): its own session kept it alive, and one
# orphan spun a core for a day. Like -m unittest after that; off Linux the prctl is a no-op.
# T-3757: with RUNPY_CPU set, the class runs under a small parent of its own, a child subreaper: whatever the class
# orphans (a killed browser's renderers) reparents to it and is reaped as it ends, and what is still running when the
# class exits is killed and reaped. Its file then gets the class's CPU: the class, all of those, its forked fm calls.
# Without fork or the prctl (not Linux) the class counts itself and the children it waited for.
BOOT = """\
import ctypes, os, signal, sys, unittest
try:
    ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
except Exception:
    pass
if sys.argv[1] != '0' and os.getppid() != int(sys.argv[1]):
    os._exit(1)  # run.py was gone before the prctl
_cpu = os.environ.pop('RUNPY_CPU', '')
if _cpu:
    import atexit, resource, time
    def _forked():
        f = sys.modules.get('forkcli')
        return f.SPENT[0] if f is not None and hasattr(f, 'SPENT') else 0.0
    def _put(path, secs):
        try:
            with open(path, 'w') as out:
                out.write(repr(secs))
        except OSError:
            pass
    try:
        ctypes.CDLL(None).prctl(36, 1)  # PR_SET_CHILD_SUBREAPER
        kid = os.fork()
    except Exception:
        kid = None
    if kid:
        import faulthandler
        faulthandler.disable()
        def _abort(*_):  # run.py's timeout: the class's stack
            try:
                os.kill(kid, signal.SIGABRT)
            except OSError:
                pass
        signal.signal(signal.SIGABRT, _abort)
        code = 1
        while True:
            try:
                pid, status = os.wait()
            except ChildProcessError:
                break
            if pid == kid:
                code = os.waitstatus_to_exitcode(status)
                break
        end = time.time() + 5
        while time.time() < end:
            kids = []
            for t in os.listdir('/proc/self/task'):
                try:
                    with open(f'/proc/self/task/{t}/children') as f:
                        kids += [int(x) for x in f.read().split()]
                except (OSError, ValueError):
                    pass
            if not kids:
                break
            for k in kids:
                try:
                    os.kill(k, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.02)
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                break
        me, them = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            with open(_cpu + '.fork') as f:
                forked = float(f.read())
        except (OSError, ValueError):
            forked = 0.0
        _put(_cpu, me.ru_utime + me.ru_stime + them.ru_utime + them.ru_stime + forked)
        if code < 0:  # the class died of a signal: so does its parent, so run.py reads the same negative code
            try:
                signal.signal(-code, signal.SIG_DFL)
            except (OSError, ValueError):
                pass  # SIGKILL and SIGSTOP can't be caught, so they need no reset
            os.kill(os.getpid(), -code)
        os._exit(code if code >= 0 else 1)
    if kid == 0:
        try:
            ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # dies with its subreaper too
        except Exception:
            pass
        atexit.register(lambda: _put(_cpu + '.fork', _forked()))
    else:
        def _alone():
            me, them = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
            _put(_cpu, me.ru_utime + me.ru_stime + them.ru_utime + them.ru_stime + _forked())
        atexit.register(_alone)
sys.path.insert(0, os.environ.get('RUNPY_TESTS') or os.getcwd())  # the tests folder, not whatever folder the class starts in
try:
    import killguard  # T-5900: a stubbed Popen's pid must never reach killpg(1): refuse it for every class, plain unittest too
    killguard.install()
except ModuleNotFoundError as e:  # run.py copied alone into a scratch folder: toy classes only (test_killguard pins the file)
    if e.name != 'killguard':
        raise
unittest.main(module=None, argv=['python -m unittest', *sys.argv[2:]])
"""


SANDBOX = []  # T-3222: run.py --sandbox, once its job sandbox is known to work here
COVER_PY = []  # T-3593: the venv python recording each class's reads (cover_python), when there is one


def sandboxed(argv, tmp, env):
    """T-3222: argv and env for a class in Sampo's job sandbox: the repo read-only, only its own temp folder
    writable, no network, credential folders empty, secret variables gone."""
    lib = os.path.join(os.path.dirname(HERE), "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from sampo import sandbox
    repo = os.path.dirname(os.path.dirname(HERE))  # T-3246: readable even under /tmp (sampo ship's scratch tree)
    return (sandbox.argv(argv, tmp, cwd=HERE, nest=True, reads=[repo]),  # nest: tests that run bwrap themselves
            sandbox.job_env(env, tmp))


def run_one(cid, k, timeout=480, deadline=None, small=False, joins=None, narrow=False):
    """T-3252: the class runs in one of the machine's CPU slots; waiting for it counts against neither its timeout nor
    its recorded time, and the run's deadline still applies once it starts. T-4247: small (the run's selection is at
    most EXPRESS_CLASSES classes) lets it take an express slot; the seconds it waited are kept in WAITED."""
    if ENDED:  # T-4399: ended from outside: not started, so no slot, no join marker and no 'red' for a class that never ran
        return cid, 1, NOT_STARTED, 0, 0.0
    wait = max(0.0, deadline - time.time()) if deadline is not None else 3600.0
    t = time.monotonic()
    with cpu_slot(wait, small=small, low=is_low(), narrow=narrow) as slot:
        got = time.monotonic()
        WAITED[cid] = WAITED.get(cid, 0.0) + got - t
        WAIT_SPANS.append((t, got))  # T-4582: kept apart for the wall-clock wait (list.append is atomic across threads)
        try:
            if joins and not joins.start(cid):  # T-4267: another run is running this class on these inputs: wait for it later
                return cid, 0, DEFERRED, 0, 0.0
            res = _run_one(cid, k, timeout, deadline, slot)
            if joins:
                joins.done(res)
            return res
        finally:
            if slot is not None:  # T-4570: the slot-seconds this class took, for the run's record
                HELD[cid] = HELD.get(cid, 0.0) + time.monotonic() - got


# T-4720: ending what a test left running. The same reading and ending is in plugin/lib/sampo/lanes.py (proc_table,
# proc_facts, under, holders, end_procs, _family), kept as a second copy because this file must run alone (an install
# has no tests folder, a checkout's lib is not on this file's path); test_leftover_procs.OneAnswer holds the two alike.
HAS_PROC = os.path.isdir("/proc/self")  # macOS has none: processes are read through lsof and ps there


def _undeleted(path):
    """T-4720: Linux reads a working folder that was removed as '<path> (deleted)'."""
    return path[:-10] if path.endswith(" (deleted)") else path


def _tool(argv, timeout=10, ok=(0,)):
    """T-4720: a program's stdout, or None when it isn't there, times out or exits with a code not in `ok`."""
    try:
        got = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return got.stdout if got.returncode in ok else None


def _lsof_table(uid):
    """T-4720: where there is no /proc (macOS): [(pid, working folder)] of the user's processes from lsof, or None."""
    out = _tool(["lsof", "-a", "-u", str(uid), "-d", "cwd", "-Fpn"], timeout=60, ok=(0, 1))
    if out is None:
        return None
    table, pid = [], None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            table.append((pid, line[1:]))
            pid = None
    return table


def proc_table(root=None, uid=None):
    """T-4720: [(pid, working folder)] of this user's processes, never another user's; None when nothing can read them
    (no /proc and no lsof), so the caller ends nothing. root and uid are for tests."""
    uid = os.getuid() if uid is None else uid
    root = root or "/proc"
    if root == "/proc" and not HAS_PROC:
        return _lsof_table(uid)
    try:
        names = os.listdir(root)
    except OSError:
        return None
    table = []
    for name in names:
        if not name.isdigit():
            continue
        d = os.path.join(root, name)
        try:
            if os.stat(d).st_uid != uid:
                continue
            table.append((int(name), _undeleted(os.readlink(os.path.join(d, "cwd")))))
        except OSError:  # gone, or not readable: not ours to end
            continue
    return table


def _family():
    """This process and its ancestors: never ended, whatever folder they stand in. Through /proc one stat read per
    ancestor; without it (macOS) one `ps -A` for them all."""
    fam, pid = {os.getpid(), os.getppid()}, os.getpid()
    parents = None
    for _ in range(64):
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            if parents is None:
                parents = {}
                for line in (_tool(["ps", "-A", "-o", "pid=,ppid="]) or "").splitlines():
                    a = line.split()
                    if len(a) == 2 and a[0].isdecimal() and a[1].isdecimal():
                        parents[int(a[0])] = int(a[1])
            pid = parents.get(pid, 0)
        if not pid or pid in fam:
            break
        fam.add(pid)
    return fam


def _terminal(link):
    return link.startswith(("/dev/pts/", "/dev/tty", "/dev/console"))


def proc_facts(pids, proc=None):
    """T-4720: {pid: (has a terminal, same mount namespace as this process, command name)} for these pids; a pid that is
    gone is left out; None when nothing can read them (no /proc and no ps), so the caller ends nothing. A terminal is
    the controlling one or one of stdin, stdout or stderr: that process is a person's (a shell, an editor, a claude
    session), never a leftover. A mount namespace that differs or can't be read is another sandbox's: its /tmp is not
    ours (a path that reads as deleted here may be live there). Same read as plugin/lib/sampo/lanes.py's."""
    if not pids:
        return {}
    if proc is None and not HAS_PROC:
        out = _tool(["ps", "-o", "pid=,tty=,comm=", "-p", ",".join(str(p) for p in pids)], ok=(0, 1))
        if out is None:
            return None
        facts = {}
        for line in out.splitlines():
            a = line.split(None, 2)
            if len(a) >= 2 and a[0].isdecimal():
                facts[int(a[0])] = (a[1] not in ("?", "??", "-"), True, os.path.basename(a[2].strip()) if len(a) > 2 else "")
        return facts
    proc = proc or "/proc"
    try:
        mine = os.readlink(f"{proc}/self/ns/mnt")
    except OSError:
        mine = None
    facts = {}
    for pid in pids:
        d = f"{proc}/{pid}"
        try:
            with open(d + "/stat") as f:
                stat = f.read()
        except OSError:
            continue
        try:
            tty = int(stat.rsplit(")", 1)[1].split()[4]) != 0
        except (IndexError, ValueError):
            tty = True  # can't tell: a person's until shown otherwise
        for fd in (0, 1, 2) if not tty else ():
            with contextlib.suppress(OSError):
                tty = tty or _terminal(os.readlink(f"{d}/fd/{fd}"))
        try:
            same = mine is None or os.readlink(d + "/ns/mnt") == mine
        except OSError:
            same = False
        facts[pid] = (tty, same, stat[stat.find("(") + 1:stat.rfind(")")])
    return facts


def under(*folders):
    """T-4720: a test on a path: is it one of these folders or inside one (a name that only starts alike is not)."""
    roots = set()
    for f in folders:
        if f and os.path.realpath(f) != os.sep:  # never the root: that would name every process
            roots.update((f.rstrip(os.sep), os.path.realpath(f)))

    def match(path):
        return any(path == r or path.startswith(r + os.sep) for r in roots)
    return match


def holders(match, table=None, facts=None, fds=False, ns=False, note=None):
    """T-4720: [(pid, folder)] of this user's processes whose working folder satisfies match (and, with fds on Linux,
    whose open files do) and that are not a person's: never this process or its ancestors, never one with a terminal
    (proc_facts), and with ns on never one in another mount namespace. note, when given, is filled {pid: (folder, command,
    has a terminal)} for every process that was judged. table and facts (a function of pids) are for tests; None reads
    the machine, and a machine that can't be read names nothing. Same answer as plugin/lib/sampo/lanes.py's holders."""
    live = table is None
    table = proc_table() if live else table
    if table is None:
        return []
    skip = _family()
    found = {pid: path for pid, path in table if pid not in skip and match(path)}
    if fds and live and HAS_PROC:  # a process that left the folder but still holds a file in it
        for pid, _ in table:
            if pid in found or pid in skip:
                continue
            try:
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    with contextlib.suppress(OSError):
                        link = _undeleted(os.readlink(f"/proc/{pid}/fd/{fd}"))
                        if link.startswith(os.sep) and match(link):
                            found[pid] = link
                            break
            except OSError:
                continue
    if not found:
        return []
    got = (proc_facts if facts is None else facts)(sorted(found))
    if got is None:
        return []
    out = []
    for pid, path in sorted(found.items()):
        tty, same, comm = got.get(pid, (False, True, ""))  # a pid with no entry is gone
        if ns and not same:
            continue
        if note is not None:
            note[pid] = (path, comm, tty)
        if not tty:
            out.append((pid, path))
    return out


def end_procs(find, grace=3.0, poll=0.1, sleep=time.sleep, kill=None):
    """T-4720: SIGTERM what find() names, SIGKILL what is left after grace s, and read again so a process that forked a
    replacement meanwhile goes too; bounded (grace, then three more rounds). find() is read afresh each round, so a pid
    that is gone or reused is not signalled by an old list. Returns the pids signalled; find() None ends nothing."""
    sent = set()
    kill = kill or os.kill  # at call time, not as a default: main() installs the pid guard (killguard) after this def runs

    def signal_all(sig):
        found = find()
        for pid, _ in found or ():
            with contextlib.suppress(OSError):  # gone already, or not ours
                kill(pid, sig)
                sent.add(pid)
        return bool(found)

    if not signal_all(signal.SIGTERM):
        return sent
    end = time.monotonic() + grace
    while time.monotonic() < end:
        sleep(poll)
        if not find():
            return sent
    for _ in range(4):
        if not signal_all(signal.SIGKILL):
            break
        sleep(poll)
    return sent


OWN_FOLDERS = set()  # T-4720: the class folders this run made and still holds (their lock is ours, so held() reads True)


def clear_folder(tmp, deep=False, lock=None):
    """T-4720: end what still lives in a test folder, then remove it (shutil.rmtree alone leaves a detached hook looping
    in the deleted folder). On Linux one /proc scan (about 5 ms; deep also reads open files, about 60 ms on a busy
    machine) and a stat read per hit. Without /proc (macOS) every read is an lsof over all processes, so nothing is read
    per class: end_run and the next run's sweep do that, once each. deep forces the read. T-4728: the caller's own lock
    (the one hold() took) is held while its processes end, then closed, then the folder goes: the lock outlives the
    processes it guards, so a sweep never sees the folder free while something still runs in it."""
    if HAS_PROC or deep:
        with contextlib.suppress(Exception):  # cleaning up never fails a class
            end_procs(lambda: holders(under(tmp), fds=deep))
    if lock:
        lock.close()
    shutil.rmtree(tmp, ignore_errors=True)


def end_run(base=None):
    """T-4720: at this run's end, what its own class folders (fmtest-<this pid>-*) still hold, open files too.
    T-4728: a folder another live run holds is not ours: a pid reused in another pid namespace that shares this temp root
    names the same prefix, and its lock says whose it is. Our own folders read as held too (our lock), so they are named."""
    base = base or tempfile.gettempdir()
    prefixes = tuple({os.path.join(b, f"{TMP_PREFIX}{os.getpid()}-") for b in (base, os.path.realpath(base))})
    own = {os.path.realpath(d) for d in OWN_FOLDERS} | set(OWN_FOLDERS)

    def mine(p):
        for pre in prefixes:
            if p.startswith(pre):
                top = pre + p[len(pre):].partition(os.sep)[0]
                return top in own or not held(top)
        return False

    with contextlib.suppress(Exception):
        end_procs(lambda: holders(mine, fds=True))


def _last_line(r):
    """T-4399: a class result's last line: the run.py line _run_one appends (a class's output above it may quote a
    nested run's text, so the text anywhere in err does not count)."""
    return r[2].rstrip().rsplit("\n", 1)[-1] if r[2] else ""


def ended_stop(r):
    """T-4399: whether a class result is one the run's end from outside stopped: killed at the end."""
    return bool(ENDED) and _last_line(r).startswith(f"run.py: {r[0]} still running when the run was {ENDED_TEXT} ")


def deadline_stop(r):
    """T-1809 (T-4399: matched on the last line, as ended_stop is): whether the run's --deadline killed this class."""
    return _last_line(r).startswith(f"run.py: {r[0]} still running at the run's deadline ")


def _wait_class(p, timeout):
    """T-4399: p.wait(timeout=timeout), but read every ENDPOLL_S whether the run was ended from outside: then it raises
    TimeoutExpired at once, as a cap's kill does (the caller says which). The class's output is a file (T-4758), so
    nothing is read here."""
    end = time.monotonic() + timeout
    while True:
        try:
            return p.wait(timeout=max(0.01, min(end - time.monotonic(), ENDPOLL_S)))
        except subprocess.TimeoutExpired:
            if ENDED or time.monotonic() >= end:
                raise


def _run_one(cid, k, timeout, deadline, slot):
    """T-1808: a class still running after timeout s is killed and fails by name, with every thread's stack
    (faulthandler on SIGABRT); its own session, so a child holding the pipes dies with it. T-1809: the run's deadline
    (an epoch) shortens that, and a class not started by then fails as NOT_STARTED."""
    t0 = time.time()
    why = f"still running after {timeout}s: killed (T-1808)"
    if ENDED:  # T-4399: the run was ended from outside: this class is not started
        return cid, 1, NOT_STARTED, 0, 0.0
    if deadline is not None:
        if deadline - t0 < 1:  # review: round() of under 0.5 s left would start the class and kill it at once
            return cid, 1, NOT_STARTED, 0, 0.0
        if deadline - t0 < timeout:  # review: said apart from the per-class cap
            timeout = round(deadline - t0)
            why = f"still running at the run's deadline ({timeout}s in): killed (T-1809)"
    extra = [a for x in k for a in ("-k", x)]
    try:
        tmp = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}{os.getpid()}-")  # T-2019: whatever the class leaks goes with it
    except OSError as e:  # T-3236: /tmp out of space or inodes fails this class by name, not the whole run
        return cid, 1, f"run.py: couldn't make the class's temp folder: {e}\n", 0, 0.0
    OWN_FOLDERS.add(tmp)
    lock = None
    try:  # T-4728: an exception before the class runs (the lock, the env, the spawn) leaks neither the lock nor the folder
        lock = hold(tmp)  # a sweep from another run (or pid namespace) never takes a folder a class is running in
        argv, env = [sys.executable, "-X", "faulthandler", "-c", BOOT, "0" if SANDBOX else str(os.getpid()), "-q", cid,
                     *extra], child_env(tmp)
        env[SLOT_ENV] = "none" if slot is None else str(slot)  # T-3442: with no slot too, its runs never queue again
        env["RUNPY_CPU"] = os.path.join(tmp, ".runcpu")  # T-3757: what the class's processes cost, written as it exits
        env["RUNPY_TESTS"] = os.path.dirname(os.path.abspath(__file__))  # T-5900: where BOOT finds killguard
        if COVER_PY:  # T-3593: its processes append the repo files they read here, the venv's python first on PATH
            env["RUNPY_COVER"] = os.path.join(tmp, ".runcover")
            env["PATH"] = os.path.dirname(COVER_PY[0]) + os.pathsep + env.get("PATH", os.defpath)
        if SANDBOX:  # its own pid namespace: it dies with bwrap (--die-with-parent), not by a parent pid check
            argv, env = sandboxed(argv, tmp, env)
        # T-4758: its stderr goes to a file, not a pipe: a process the class leaves behind in its own session keeps the pipe
        # open, and without a subreaper (macOS) nothing ends it with the class, so the read of the pipe waited for the
        # class's whole timeout and, after the kill, for ever (RunSweeps took 1218 s there). A wait on the class itself ends
        # when the class does.
        errf = open(os.path.join(tmp, ".runerr"), "wb")
        try:
            p = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.DEVNULL, stderr=errf, stdin=subprocess.DEVNULL,
                                 start_new_session=True, env=env)
        finally:
            errf.close()
    except BaseException:
        if lock:
            lock.close()
        shutil.rmtree(tmp, ignore_errors=True)
        OWN_FOLDERS.discard(tmp)
        raise
    LIVE_CLASSES.add(p.pid)  # T-4538: watch_parent kills it if this run's starter is gone
    tail = ""
    try:
        _wait_class(p, timeout)  # T-4758 (no pipes: a wait on the class) with T-4399's end poll
    except subprocess.TimeoutExpired:
        if ENDED:  # T-4399: named as the end's, not as a timeout's: 'not run', never 'failed'
            why = f"still running when the run was {ENDED_TEXT} ({signal.Signals(ENDED[0]).name}): killed (T-4399)"  # ended_stop reads this line
        p.send_signal(signal.SIGABRT)  # the stack, to stderr
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        tail = f"\nrun.py: {cid} {why}\n"
    else:  # passed or failed on its own: what it left in its process group goes now (no subreaper to sweep it on macOS)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, signal.SIGKILL)
    finally:
        LIVE_CLASSES.discard(p.pid)  # T-4538: after p.wait reaped it, so the pid cannot name another group
        if COVER_PY:
            with contextlib.suppress(OSError), open(os.path.join(tmp, ".runcover"), encoding="utf-8",
                                                    errors="surrogateescape") as f:
                COVERED[cid] = f.read()
        with contextlib.suppress(OSError, ValueError), open(os.path.join(tmp, ".runcpu")) as f:
            CPU[cid] = float(f.read())
        with contextlib.suppress(OSError), open(os.path.join(tmp, ".runerr"), encoding="utf-8", errors="replace") as f:
            tail = f.read() + tail  # T-4758: read before the folder goes
        clear_folder(tmp, lock=lock)  # T-4720: what a class's git hook left running in it ends first; T-4728: then the lock
        OWN_FOLDERS.discard(tmp)
    err = tail
    m = re.search(r"Ran (\d+) tests?", err)
    return cid, p.returncode, err, int(m.group(1)) if m else 0, time.time() - t0  # killed: negative


def kill_orphans():
    """T-4829: SIGKILL each live class's group once its starter is gone. A killpg that fails is said on stderr with its
    errno and the pgid, not swallowed: CI's macOS orphan check reported only 'its class kept running'."""
    for pid in list(LIVE_CLASSES):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError as e:
            sys.stderr.write(f"run.py: killpg({pid}, SIGKILL) failed: errno {e.errno} ({e.strerror})\n")
            sys.stderr.flush()


def watch_parent(poll=PARENT_POLL_S):
    """T-4538: a run whose starter has exited (a check killed mid-run; nobody reads its result) ends now, its classes
    with it, so their CPU slots are freed. A run started detached on purpose sets SAMPO_KEEP_ORPHAN=1 and keeps going.
    The starter is compared with the one at startup, not with pid 1: a subreaper adopts an orphan, not init."""
    if _env("SAMPO_KEEP_ORPHAN"):
        return
    parent = os.getppid()

    def watch():
        while True:
            time.sleep(poll)
            if os.getppid() != parent:
                kill_orphans()
                end_run()  # T-4720: atexit does not run after os._exit
                os._exit(1)

    threading.Thread(target=watch, daemon=True, name="run-parent-watch").start()


RUNLOCK = ".runlock"


def hold(folder):
    """T-4728: the open lock file a live run keeps on its temp folder (flock, released when the file closes or the process
    ends, however it ends); None where flock isn't there. A pid says nothing of a run in another pid namespace that shares
    this temp root (its pid reads as dead, or as another process's), a lock does."""
    if fcntl is None:
        return None
    try:
        f = open(os.path.join(folder, RUNLOCK), "w")
        fcntl.flock(f, fcntl.LOCK_EX)
    except OSError:
        return None
    return f


def held(folder):
    """T-4728: True when a live run holds `folder`'s lock. A folder with no lock file is nobody's."""
    if fcntl is None:
        return False
    try:
        with open(os.path.join(folder, RUNLOCK)) as f:  # read only: it never creates the file
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except OSError:
        pass
    return False


def sweep(base=None):
    """T-2019: a run killed before its cleanup leaves its classes' temp dirs; the next run removes dead runs' ones.
    T-4728: dead means its pid is gone and nobody holds its lock; a removal is only ever a real folder directly under the
    temp root (never a link, never a name that resolves elsewhere)."""
    base = base or tempfile.gettempdir()
    dead = []
    root = os.path.realpath(base)
    for name in os.listdir(base):
        m = re.match(rf"(?:{TMP_PREFIX}|fmfork-)(\d+)-", name)  # T-3572: and a run's fork server's folder
        if not m:
            continue
        path = os.path.join(base, name)
        if os.path.islink(path) or os.path.dirname(os.path.realpath(path)) != root:
            continue
        try:
            os.kill(int(m.group(1)), 0)
        except ProcessLookupError:
            if not held(path):  # T-4728: a held folder is a live run's, so nothing in it is a leftover (T-4720)
                dead.append(path)
        except PermissionError:
            pass  # alive, someone else's
    if dead:  # T-4720: one read for all of them; a hook that outlived its run must not loop on in a removed folder
        with contextlib.suppress(Exception):
            end_procs(lambda: holders(under(*[d for d in dead if not held(d)]), fds=True, ns=True))  # another sandbox's /tmp is not a dead run's
    for d in dead:
        if not held(d):  # looked at again: a run may have taken the lock since the listing
            shutil.rmtree(d, ignore_errors=True)


def slowest(results, n):
    return ", ".join(f"{'.'.join(c.split('.')[1:][-2:])} {s:.0f}s" for c, _, _, _, s in sorted(results, key=lambda r: -r[4])[:n])


def summary_notes(results, elapsed, deadline):
    """T-1809: the run's time and five slowest classes as a public ::notice, a ::warning past 75% of the deadline,
    and one ::error for the classes the deadline left unstarted."""
    notes = [f"::notice title=run.py::Ran {sum(r[3] for r in results)} tests in {elapsed:.0f}s; "
             f"slowest: {slowest(results, 5)}"]
    if deadline and elapsed > 0.75 * deadline:
        notes.append(f"::warning title=run.py::the suite took {elapsed:.0f}s, {100 * elapsed / deadline:.0f}% of its "
                     f"{deadline}s deadline: speed up the slowest classes or raise the job's timeout")
    unstarted = sum(r[2] == NOT_STARTED for r in results)
    if unstarted:
        notes.append(f"::error title=run.py::{unstarted} classes never started: the run reached its {deadline}s deadline")
    broke = [f"{r[0]} (exit {r[1]})" for r in results if r[1] != 0 and r[2] != NOT_STARTED]
    if broke:  # T-1911: one line naming them all, should a class's own annotations go missing
        notes.append(f"::error title=run.py::{len(broke)} classes failed: {', '.join(broke)}"[:1000])
    return notes


@contextlib.contextmanager
def burn(n):
    """T-1730: n busy-loop children for the block's duration (a load flake on demand), killed after; each also stops
    by itself once its parent is gone (a run killed with SIGKILL leaves no spinner behind)."""
    procs, spin = [], f"import os\nwhile os.getppid() == {os.getpid()}: pass"
    try:
        for _ in range(max(0, n)):
            procs.append(subprocess.Popen([sys.executable, "-c", spin], stdin=subprocess.DEVNULL))
        yield procs
    finally:
        for p in procs:
            p.kill()
        for p in procs:
            p.wait()


LOAD_BAR = 1.5  # T-3936: as core.LOAD_BAR: load per CPU past which a cap-kill is a busy host's, retried alone


def load_per_cpu():
    return os.getloadavg()[0] / (os.cpu_count() or 1) if hasattr(os, "getloadavg") else 0.0


def load_room(n, cpus=None, load=None, spinners=None):
    """T-3441: how many of --load's n busy loops fit under twice the CPU count, given the 1-minute load and the burn
    spinners already running (counted from /proc: the average lags a run started seconds ago)."""
    cpus = cpus or os.cpu_count() or 4
    if load is None:
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    if spinners is None:
        spinners = 0
        for pid in os.listdir("/proc") if os.path.isdir("/proc") else ():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    spinners += b"while os.getppid() == " in f.read()
            except OSError:
                continue
    return max(0, min(n, int(2 * cpus - max(load, spinners))))


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # T-3252: the pool bounds this run's workers; T-4531: this run gets only the CPUs it may use (cgroup cap, affinity)
    ap.add_argument("-j", type=int, default=min(slot_count(), usable_cpus(_cgroup_dir())) or usable_cpus(_cgroup_dir()))
    ap.add_argument("-k", action="append", default=[], help="only tests whose id contains this (repeatable)")
    ap.add_argument("-p", action="append", help="module glob, as unittest discover's -p (repeatable; default "
                                                "test*.py)")  # T-0389: a second -p replaced the first
    ap.add_argument("--load", type=int, default=0, metavar="N",
                    help="run beside N busy-loop processes (T-1730: reproduce a flake that shows only under load)")
    ap.add_argument("--timeout", type=int, default=480, metavar="S",
                    help="kill a test class still running after S seconds and fail it with its stack (T-1808)")
    ap.add_argument("--deadline", type=int, metavar="S",
                    help="end the whole run after S seconds: running classes are killed with their stacks, unstarted "
                         "ones fail (T-1809: set it below a CI job's cap, so the run names what was slow)")
    ap.add_argument("--shard", metavar="K/N",
                    help="run only the Kth of N interleaved slices of the classes (T-1842: CI splits the suite "
                         "across N jobs; biggest-first order keeps the slices even)")
    ap.add_argument("--sandbox", action="store_true", help="run each class in Sampo's job sandbox (T-3222): the repo "
                    "read-only, only its own temp folder writable, no network; plain, with a note, where it can't run")
    ap.add_argument("--last", action="store_true", help="print the newest record of a run written from this worktree (red: "
                    "each failed test and its first line; green: its test count, and the selection it ran) and run "
                    "nothing, reading no tree (T-4268); exit 3 when none")
    ap.add_argument("--same-tree", action="store_true", help="with --last: the newest record of a run on this tree's "
                    "content now, from any worktree (reads the tree)")
    ap.add_argument("--any-tree", action="store_true", help="with --last: the newest record of any worktree of this repo")
    ap.add_argument("--run-id", metavar="ID", help="with --last: the record stamped with this id (the check's, "
                    "SAMPO_TEST_RUN_ID), of any worktree")
    ap.add_argument("--json", action="store_true", help="with --last: the whole record as JSON")
    ap.add_argument("--held", action="store_true", help="print the slot-seconds each kind of run (priority gate, post-merge "
                    "verify, other) took on a UTC day, from this repo's run records, and run nothing (T-4570)")
    ap.add_argument("--day", metavar="YYYY-MM-DD", help="with --held: the UTC day (default today)")
    ap.add_argument("--known-red", action="append", metavar="CLASS", help="print, as JSON, {class: age in s} of the named "
                    "classes (test_x.Class, repeatable) the run records show red on this tree's inputs now, and run "
                    "nothing (T-5912: the land skips a leftover whose close failed only on these)")
    ap.add_argument("--estimate", action="store_true", help="print, as JSON, the recorded seconds of the named modules' "
                                                            "classes and the CPU slots (T-4892); runs nothing")
    ap.add_argument("names", nargs="*", help="test module names (T-0808: fm check affected's {names}); with -k, only "
                                             "their tests that match a -k (T-1994)")
    a = ap.parse_intermixed_args(argv)  # names may sit on either side of -k
    for i, n in enumerate(a.names):  # T-3428: a path into the tests dir names its module
        # T-5588: `module::Class` (the module as below) selects that one class: its id as t.id() holds it, "mod.Class."
        mod, sep, cls = n.partition("::")
        if sep and not (mod and cls.isidentifier()):
            ap.error(f"{n}: a class id is module::Class, like test_run::Args")
        if "/" in mod or mod.endswith(".py"):
            d, f = os.path.split(mod)
            if not (f.startswith("test") and f.endswith(".py") and (not d or os.path.realpath(d) == os.path.realpath(HERE))):
                ap.error(f"{n}: not a test name or a test_*.py file in {HERE}")
            mod = f[:-3]
        if sep:
            if not mod.isidentifier():
                ap.error(f"{n}: a class id is module::Class, like test_run::Args")
            mod += f".{cls}."
        a.names[i] = mod
    if a.deadline is not None and a.deadline < 1:  # review: 0 is falsy and turned the deadline off
        ap.error("--deadline takes a number of seconds, 1 or more")
    if a.shard is not None:
        k, _, n = a.shard.partition("/")
        if not (k.isdigit() and n.isdigit() and 1 <= int(k) <= int(n)):
            ap.error("--shard takes K/N with 1 <= K <= N, like 1/2")
        a.shard = int(k), int(n)
    a.error = ap.error
    return a


def main():
    try:  # T-3503: a worker killed with SIGABRT must not leave a core dump; children inherit the limit
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    try:
        import killguard  # T-5900: the runner's own killpg calls (a hung class, a leftover) take a real pid or raise
        killguard.install()
    except ModuleNotFoundError as e:  # run.py copied alone into a scratch folder (test_check_paths): toy classes only
        if e.name != "killguard":
            raise
    a = parse()
    ap = a
    if a.last:  # T-4268: a reader: no slot, no sweep, no test
        return show_last(a)
    if a.held:  # T-4570: a reader too
        return show_held(a)
    if a.known_red:  # T-5912: a reader too: the land asks which of its skipped leftovers' classes are still red on trunk
        return show_known_red(a)
    if a.estimate:  # T-4892: a reader: what the named selection is known to cost, for the check that sizes its deadline
        print(json.dumps(estimate(a.names, load_times(), slot_count())))
        return 0
    return _with_end_handlers(_suite, a)  # T-4399: the readers above return with the default signals (a kill ends them)


def _suite(a):
    """a suite run (the readers were answered in main): its classes, its report, its exit code."""
    ap = a
    watch_parent()  # T-4538: a run whose starter is gone ends with its classes
    run_id = _env(RUN_ID_ENV)  # T-4268: the check's id for this gate: kept for the record, out of every class's environment
    for k in (RUN_ID_ENV, "FOREMAN_" + RUN_ID_ENV[len("SAMPO_"):]):
        os.environ.pop(k, None)
    if any(re.search(r"\s", k) for k in a.k + a.names):  # T-0357: '-k "a or b"' matched nothing and failed only at the end
        ap.error("-k matches a substring of test ids; repeat -k for each pattern (-k test_guard -k test_cli)")
    if a.sandbox:
        lib = os.path.join(os.path.dirname(HERE), "lib")
        sys.path.insert(0, lib)
        from sampo import sandbox
        ok, why = sandbox.available()
        if ok:
            SANDBOX.append(why)
        else:
            sys.stderr.write(f"run.py: --sandbox: not available here ({why}); running plain\n")
    t0 = time.time()
    sweep()
    atexit.register(end_run)  # T-4720: what this run's class folders still hold when it ends
    hit, ids = set(), []
    parts, whole = {}, set()
    todo = classes(a.p or ["test*.py"], a.k, hit, a.names, ids, parts, whole)
    missing = [n for n in a.names if n not in hit]
    if missing:  # T-1287: a name that matches nothing (a test file that doesn't exist) fails, not passes unseen
        fold = lambda s: s.lower().replace("_", "")  # noqa: E731
        near = dict.fromkeys(seg for i in ids if any(n in i for n in missing) for seg in i.split(".")
                             if any(fold(x) in fold(seg) for x in a.k))  # T-2003: -k core_integrity meant CoreIntegrity
        shown = [n[:-1].replace(".", "::", 1) if n.endswith(".") else n for n in missing]  # T-5588: a class id as typed
        sys.stderr.write(f"run.py: no test matches {', '.join(shown)}"  # T-1999: say what filtered it out
                         + (f" with -k {' -k '.join(a.k)}" if a.k else "")
                         + (f"; did you mean {' or '.join('-k ' + s for s in list(near)[:3])}?" if near else "") + "\n")
        return 1
    if a.shard:
        todo = todo[a.shard[0] - 1::a.shard[1]]
    # T-3502: a run naming modules skips the classes green at the same code, but 1 in 10 drawn; every run records
    # them. Not with -k (part of a class), switched off, or nested in a class's slot (a test's own run.py).
    # T-3593: "the same code" is what the class read when it passed (its coverage), so a change elsewhere skips it
    # T-3638: the post-merge verify alone sets SAMPO_CACHE_FULL=1 to let a run naming no modules skip too; taken out
    # of the environment here so no class or child run inherits it (ship and a plain full run never skip)
    incr = (_env(CACHE_FULL) or "0") != "0"
    for k in (CACHE_FULL, "FOREMAN_" + CACHE_FULL[len("SAMPO_"):]):
        os.environ.pop(k, None)
    # T-3757: the train's gate and the post-merge verify name a baseline; out of the environment too, so no class or
    # child run compares or records. Not for -k or a shard (part of the suite).
    cpu_base, cpu_gate = os.environ.pop(CPU_BASE, ""), os.environ.pop(CPU_GATE, "") not in ("", "0")
    cpu_base = "" if a.k or a.shard else cpu_base
    hist, cached_cpu, cpu_red = (load_cpu(cpu_base)["classes"] if cpu_base else {}), {}, []
    skip, drawn, got, ident, joined = {}, set(), None, {}, {}
    if not (a.k or (_env(NO_CACHE) or "0") != "0" or _env(SLOT_ENV)):
        got = tree()
    rtree, end_tree = got, None  # T-4268: the tree the cache read, for the record: a run that read none records no content id
    if got:
        cache, value = cache_path(got[2]), values(*got[:2])
        COVERED.clear()
        gen, recs = load_cache(cache)
        rec = {c: r for c in todo for r in recs.get(c, ())[::-1] if r[0] == digest(r[2], value)} if a.names or incr else {}
        hits = {c: r[1] for c, r in rec.items()}
        drawn = {c for c in hits if random.random() < sample_rate(incr)}  # T-5588: none in a lane, unless the full gate
        skip = {c: n for c, n in hits.items() if c not in drawn}
        if cpu_base:  # T-3757: a cached class costs what it cost then; one over its margin runs, so it is measured
            known = {c: rec[c][3] for c in skip if len(rec[c]) > 3}
            over = {c for c, *_ in cpu_over(hist, known, BUDGETS)}
            skip = {c: n for c, n in skip.items() if c not in over}
            cached_cpu = {c: s for c, s in known.items() if c in skip}
        todo = [c for c in todo if c not in skip]
        if (_env(NO_JOIN) or "0") == "0":  # T-4267: what a class would read, by its newest record, on this tree: its inputs' identity
            ident = {c: digest(recs[c][0][2], value) for c in todo if recs.get(c)}
        if skip:
            sys.stderr.write(f"run.py: skipped {len(skip)} classes green at the same code; "
                             "FOREMAN_NO_TEST_CACHE=1 runs them\n")
        if not SANDBOX and (py := cover_python()):
            COVER_PY.append(py)
            sys.executable = py  # every Python run.py starts records, a fork server it starts (T-3572) too
            os.environ["RUNPY_COVER_ROOTS"] = os.pathsep.join(_roots(got[0]))  # what runcover.py records
    owner = lambda j: j if j in parts else j.rsplit(".", 1)[0]  # noqa: E731  (a split class's job is one test)
    times = {} if a.shard else load_times()  # a shard's slice must not depend on this machine's last run
    todo = plan(todo, parts, whole, times)  # T-3166: jobs, a big class's tests apart
    known = {}  # T-4269: class -> age (s) of the record that shows it red on these inputs; those jobs go first, none is dropped
    if got:
        try:
            known = known_red(got[2], {owner(j) for j in todo}, value, known_red_window())
        except Exception as e:  # a record that can't be read only costs the order
            sys.stderr.write(f"run.py: the run records were not read for known red classes (T-4269): {repr(e)[:200]}\n")
    if known:
        todo = sorted(todo, key=lambda j: owner(j) not in known)  # stable: the rest keep their longest-first order
        lo, hi = (round(f(known.values()) / 60) for f in (min, max))
        sys.stderr.write(f"run.py: known red on these inputs (recorded {f'{lo}-{hi}' if lo != hi else lo} min ago): running first: "
                         f"{', '.join(sorted(known))}\n")
    if a.load and (room := load_room(a.load)) < a.load:
        sys.stderr.write(f"run.py: --load {a.load} cut to {room}: the host already runs near twice its CPU count\n")
        a.load = room
    try:  # T-3572: one fork server answers every class's fm calls
        import forkcli
    except ImportError:  # a copy of run.py alone (test_check_paths) has no forkcli
        forkcli = None
    if forkcli and SANDBOX:  # a sandboxed class can't reach a server outside its sandbox
        os.environ.pop(forkcli.SHARED, None)
    elif forkcli:
        forkcli.share()  # stopped and waited for at exit, so the run's rusage counts its forked calls
    try:
        import helpers  # T-3338: lazily, after classes() put the tests folder on sys.path
        busy = helpers.busy_factor()
    except ImportError:  # a copy of run.py alone (test_check_paths) has no helpers
        busy = 1
    cap = round(a.timeout * busy)  # a loaded host's class runs slower; the deadline still clamps
    # T-3338 review: --deadline, else the gate's (core.run_command sets FM_RUN_DEADLINE under its own timeout), else
    # 8 class timers: the scaled timers never outlast the caller's, so a kill names the class
    dl = a.deadline if a.deadline is not None else int(os.environ.get("FM_RUN_DEADLINE") or 0) or max(8 * cap, 600)
    a.deadline, end = dl, t0 + dl
    # T-3541: the whole suite (the gate: no names, -k or -p) waits for any other on the machine, at most half its deadline
    slot = suite_slot(t0 + dl / 2) if not (a.names or a.k or a.p) else contextlib.nullcontext()
    # T-4247: at most two classes to run: they may take an express slot; T-4650: so may a few classes of little work
    small = small_selection((owner(j) for j in todo), todo, times, parts)
    narrow = is_narrow(a.names, a.k, a.p)  # T-4685: a handful of named modules may also take the reserve's idle slots
    # T-4267: a class another run is running on the same inputs is that run's: this one does its other classes, then waits
    joins = None
    if ident:
        base = shared(*got[:2])

        def read(jobs):  # what a class's jobs read in this run, as its cache record would name it; None: unrecorded
            texts = [COVERED.get(j) for j in jobs]
            return None if None in texts else sorted(run_paths(  # T-5484: as the cache's record names them
                base, texts, *got[:2], READS.get(owner(jobs[0])) if jobs else None))

        def fresh():  # the tree's values now; None when it can't be read or is another repo's
            again = tree()
            return values(*again[:2]) if again and again[0] == got[0] else None
        joins = Joins(cache, ident, value, owner, todo, times, {c: len(parts.get(c, ())) for c in ident},
                      {c: recs[c][0][2] for c in ident}, read, fresh, t0)
    with slot, burn(a.load), concurrent.futures.ThreadPoolExecutor(max(1, a.j)) as ex:
        told, told_lock = set(), threading.Lock()  # T-4269: the known-red classes whose second red was said (once each)

        def go(c):  # T-1994: the class is in a named module, so -k alone
            r = run_one(c, a.k or a.names, cap, end, small, joins, narrow)
            if (known and r[1] != 0 and owner(r[0]) in known and r[2] not in (NOT_STARTED, DEFERRED)
                    and not deadline_stop(r) and not ended_stop(r)):
                with told_lock:  # red again on the inputs its record had it red on: said now, not at the end
                    first = owner(r[0]) not in told
                    told.add(owner(r[0]))
                if not first:
                    return r
                f = fail_entries([r], owner, [])[0]
                sys.stderr.write(f"run.py: known red again: {f['class']}{'.' + f['test'] if f['test'] else ''}"
                                 f"{': ' + f['first'] if f['first'] else ''}\n")
            return r
        results = list(ex.map(go, todo))
        if joins:
            theirs = joins.deferred()
            results = [r for r in results if r[2] != DEFERRED]
            if theirs:
                joined = joins.wait(end)
                if cpu_base:  # T-3757: a joined class costs what it cost its owner; one over its margin runs here, measured
                    known = {c: joins.cpu[c] for c in joined if c in joins.cpu}
                    over = {c for c, *_ in cpu_over(hist, known, BUDGETS)}
                    joins.take(over)
                    joined = {c: n for c, n in joined.items() if c not in over}
                    cached_cpu.update({c: s for c, s in known.items() if c in joined})
                results += list(ex.map(go, [j for j in todo if owner(j) in theirs and owner(j) not in joined]))
        retried = set()
        for i, r in enumerate(results):  # T-3936: a cap-kill on a loaded host runs once more alone, the pool drained
            if r[1] < 0 and "killed (T-1808)" in r[2] and (lp := load_per_cpu()) > LOAD_BAR:
                again = go(r[0])
                if again[1] == 0:
                    sys.stderr.write(f"run.py: {r[0]} passed alone (load {lp:.1f} per CPU): "
                                     f"its kill at the cap was the host's\n")
                    results[i] = again
                retried.add(r[0])
        for i, r in enumerate(results):  # T-4442: a class red while the host is past the bar runs once more alone: a
            # loaded host fails timing-sensitive classes the code did not (T-4358: 16 of 17 green alone). The drawn
            # classes have their own rerun below, a cap-kill had its, and a deadline's cut is not a failure.
            if (r[1] != 0 and r[0] not in retried and owner(r[0]) not in drawn and r[2] != NOT_STARTED
                    and not deadline_stop(r) and not ended_stop(r) and not ENDED and os.environ.get("FOREMAN_LOAD_RERUN") != "0"
                    and (lp := load_per_cpu()) > LOAD_BAR):  # FOREMAN_LOAD_RERUN=0: a red class is never run twice
                again = go(r[0])
                if again[1] == 0:
                    results[i] = again
                    if got:  # no cache read (-k, FOREMAN_NO_TEST_CACHE): no events file either
                        note_event(cache, "flake", r[0])
                    sys.stderr.write(f"run.py: {r[0]} load flake: failed once, passed alone (load {lp:.1f} per CPU)\n")
        bad = [r[0] for r in results if r[1] != 0 and owner(r[0]) in drawn  # a deadline's stop says nothing of it
               and r[2] != NOT_STARTED and not deadline_stop(r) and not ended_stop(r) and not ENDED]
        for i, r in enumerate(results):  # T-4274: red on code the cache has green is a flake (a loaded host fails the
            if r[0] in bad:              # timing-sensitive classes) far more often than a file the coverage missed:
                again = go(r[0])         # it runs once more, the pool drained, before the cache is blamed
                if again[1] == 0:
                    results[i] = again
                    bad.remove(r[0])
                    note_event(cache, "flake", r[0])
                    sys.stderr.write(f"run.py: {r[0]} failed once and passed again on code the cache has green (load "
                                     f"{load_per_cpu():.1f} per CPU): a flake, the cache stands\n")
                elif again[2] == NOT_STARTED or deadline_stop(again) or ended_stop(again):
                    bad.remove(r[0])  # the deadline's or the end's stop says nothing of the cache; the first red stands
        if bad and not ENDED:  # T-3502 (T-4399: an ended run's reds say nothing of the cache): green in the cache at this code, red twice now: the cache is wrong, so nothing it said holds
            note_event(cache, "drop", bad[0])
            drop_cache(cache)
            sys.stderr.write(f"run.py: {bad[0]} failed though the cache had it green at the same code: the cache is "
                             f"wrong, dropped" + (f"; running the {len(skip)} classes it skipped\n" if skip else "\n"))
            more = plan(list(skip), parts, whole, times)
            results += list(ex.map(go, more))
            todo, skip, got, cached_cpu = todo + more, {}, None, {}
        now = class_cpu(results, owner)
        if cpu_base and (redo := [c for c, *_ in cpu_over(hist, {**cached_cpu, **now}, BUDGETS) if c in now]):
            # T-3757: a busy machine inflates CPU time too, so a class over its margin runs once more and the lower
            # count stands
            again = class_cpu(list(ex.map(go, [r[0] for r in results if owner(r[0]) in redo])), owner)
            now.update({c: min(now[c], again[c], key=lambda x: x[0]) for c in redo if c in again})
        over = cpu_over(hist, {**cached_cpu, **now}, BUDGETS) if cpu_base else []
        if over and not cpu_gate:  # the post-merge verify: what landed is shown, and becomes the new usual
            sys.stderr.write("".join(f"run.py: CPU over its margin, not gated: {c} took {s:.1f} CPU s, {why}\n"
                                     for c, s, _, why in over))
        cpu_red = over if cpu_gate else []
    # T-4269: a class its record had red on these inputs that now passes in every job it ran: a flaky one. The run's
    # exit follows the results alone; the record says 'cleared' so the next run does not take it for red again.
    flaky = sorted(c for c in known if (js := [r for r in results if owner(r[0]) == c]) and all(r[1] == 0 for r in js))
    for c in flaky:
        sys.stderr.write(f"run.py: was red, now green on the same inputs: {c} (flaky?)\n")
    if got:  # T-3502: each class that ran whole and green, none skipped, recorded with what it read (T-3593)
        size, ran, cov = {}, {}, {}
        for t in set(ids):
            size[t.rsplit(".", 1)[0]] = size.get(t.rsplit(".", 1)[0], 0) + 1
        for job, code, err, n, _ in results:  # a skip (skipUnless a browser) passes here, maybe not elsewhere
            ok, m = ran.get(owner(job), (True, 0))
            ran[owner(job)] = ok and code == 0 and not re.search(r"\bskipped=\d", err), m + n
            cov.setdefault(owner(job), []).append(COVERED.get(job))  # None: a job nothing recorded for
        new = {c: n for c, (ok, n) in ran.items()
               if ok and None not in cov[c] and n == size.get(c) == len(parts.get(c, ()))}
        again = end_tree = tree() if new else None  # a file edited while the run ran: its class ran the old code or the new
        if again and again[0] == got[0]:
            base, after = shared(*got[:2]), values(*again[:2])
            paths = {c: sorted(run_paths(base, cov[c], *got[:2], READS.get(c))) for c in new}
            new = {c: [digest(paths[c], value), n, paths[c], *([now[c]] if c in now else [])] for c, n in new.items()}
            new = {c: r for c, r in new.items() if digest(r[2], after) == r[0]}
            if new:
                save_cache(cache, new, gen, causes(cov, new))  # T-5484: why each whole-tree record reruns always
    if joins:
        joins.close()  # the last greens said; the markers of classes it never finished go
    if not a.shard:
        seen = set(ids)
        # T-3338: a killed class (negative rc) counts as slow, so the next run splits it instead of killing it again
        timer = lambda r: r[1] < 0 and "killed (T-1808)" in r[2]  # noqa: E731  # not a deadline kill or a SIGKILL
        new = {r[0]: max(r[4], SPLIT_S) if timer(r) else r[4]
               for r in results if (r[1] >= 0 or timer(r)) and r[2] != NOT_STARTED and r[0] in seen | set(parts)}
        for cid, js in parts.items():  # a split class's work, so the next run still knows if it is slow
            if js and all(j in new for j in js):
                new[cid] = work_time([new[j] for j in js])
        if a.k:  # a -k run ran part of each class: its time is no class's
            new = {j: s for j, s in new.items() if j not in parts}
        if new:
            save_times(new)
    failed = [r for r in results if r[1] != 0]
    if cpu_base and not failed and not cpu_red and now:  # T-3757: a green run is the next usual
        record_cpu(cpu_base, now)
    ci = os.environ.get("GITHUB_ACTIONS")  # T-1223: a run's log needs a login; its annotations are public
    for cid, _, err, _, _ in failed:
        if err == NOT_STARTED:
            continue  # one line for all of them, below
        sys.stderr.write(f"\n==== {cid}\n{err}")
        if ci:
            print("\n".join(annotations(cid, err)), flush=True)
    if ENDED:  # T-4399: one line says the end was an outside kill, what was killed and what was not started
        sys.stderr.write(f"\nrun.py: {ENDED_TEXT} ({signal.Signals(ENDED[0]).name}) after {time.time() - t0:.0f} s: "
                         "the classes still running were killed, the classes not started were not run\n")
    late = [r[0] for r in failed if r[2] == NOT_STARTED]
    if late:  # review: outside CI too, which classes the deadline left out
        why_late = ENDED_TEXT if ENDED else f"the run's {a.deadline}s deadline"
        sys.stderr.write(f"\nrun.py: not started ({why_late}): {', '.join(late)}\n")
    if ci:
        print("\n".join(summary_notes(results, time.time() - t0, a.deadline)), flush=True)
    total = sum(r[3] for r in results)
    cached = f"; {sum(skip.values())} more green in the cache" if skip else ""  # T-3502: core._SOME_TESTS reads it
    cached += f"; joined {len(joined)} ({sum(joined.values())} tests green in a run beside this one)" if joined else ""  # T-4267: so does it
    cached += f"; flaky {len(flaky)} (red before on these inputs, green now)" if flaky else ""  # T-4269
    slow = slowest(results, 3)
    if skipped():
        sys.stderr.write(f"\nleft out (FOREMAN_SKIP_TESTS): {', '.join(sorted(skipped()))}")
    for cid, s, usual, why in cpu_red:  # T-3757: a red class like any other, so the train's probes narrow to it
        sys.stderr.write(f"\n==== {cid}\nrun.py: CPU over its margin (T-3757): {s:.1f} CPU s, {why}"
                         + (f" ({usual:.1f} s)" if usual is not None else "") + "\n")
    # T-3936: a class the deadline cut is 'not run', never 'failed': the host was slow, nothing regressed
    stopped = [r for r in failed if r[2] == NOT_STARTED or deadline_stop(r) or ended_stop(r)]
    real = [r for r in failed if r not in stopped]
    names = [r[0] for r in real] + [c for c, *_ in cpu_red if c not in {r[0] for r in real}]
    said = ", ".join(names[:5]) + (f", +{len(names) - 5} more" if len(names) > 5 else "")
    try:  # T-4268: the run's record, red or green, for `run.py --last` and the check's naming of what failed; never the
        # run's result (not for a test's own run.py in a slot, nor outside git)
        if not _env(SLOT_ENV) and (where := _where()):
            entire = not (a.names or a.k or a.p or a.shard or skipped())  # else a selection: its green says less
            sel = " ".join(sys.argv[1:]) + (f" (left out: {', '.join(sorted(skipped()))})" if skipped() else "")
            end_tree = end_tree or (tree() if rtree else None)  # the tree after the run: a test may leave a file
            ents = fail_entries(real, owner, cpu_red)
            ents = safe_entries(ents, _redactor() if any(e["text"] or e["first"] for e in ents if not e.get("own")) else None)
            made = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "t": round(time.time(), 3),
                    "worktree": where[0], "tree": tree_id(rtree[1]) if rtree else "",
                    "tree_end": tree_id(end_tree[1]) if end_tree else "", "run_id": run_id or "",
                    "argv": sys.argv[1:], "whole": entire, "selection": "" if entire else sel.strip(),
                    "ok": not failed and not cpu_red and bool(total or skip or joined) and not ENDED, "ran": total,
                    "cached": sum(skip.values()), "joined": len(joined),  # T-5557: a run whose every class was joined is green
                    "n_failed": len(names), "n_not_run": len(stopped), **slot_fields(t0, time.time()),
                    "classes": len({owner(r[0]) for r in results}),  # T-4491: the deadline line says 'N of M classes ran'
                    "classes_not_run": len({owner(r[0]) for r in stopped}),
                    "failed": ents[:FAILS_MAX],
                    "not_run": [r[0] for r in stopped][:NOT_RUN_MAX]}
            if rtree:  # T-4269: what each red class read, so a later run on the same inputs knows it red; and the flaky ones
                made["inputs"] = red_inputs([e["class"] for e in ents if not e.get("own")], real, owner,
                                            shared(*rtree[:2]), *rtree[:2], value)
                made["cleared"] = flaky
            record_run(where[1], {**made, "lines": record_lines(made)})  # the lines too: a caller prints them as they are
    except Exception as e:
        sys.stderr.write(f"run.py: the run's record was not written (T-4268): {repr(e)[:200]}\n")
    sys.stderr.write("".join(f"\n  failed class: {n}" for n in names) + ("\n" if names else ""))  # T-3397: all of them, one per line
    me = resource.getrusage(resource.RUSAGE_SELF) if resource else None
    spent = sum(CPU.values()) + (me.ru_utime + me.ru_stime if me else 0)  # T-3757: the classes' own count, and run.py's
    sys.stderr.write(f"\nRan {total} tests in {time.time() - t0:.1f}s ({len(todo)} jobs, {a.j} at a time; "
                     f"slowest: {slow}; cpu {spent:.1f} s{slot_notes(WAITED, WAIT_SPANS, left_now(), time.time() - t0)}){cached}\n\n"
                     f"{f'FAILED ({len(names)} classes: {said})' if names else 'INCOMPLETE (ended from outside)' if ENDED else 'INCOMPLETE (stopped at its deadline)' if stopped else 'OK' if total or skip or joined else 'NO TESTS RAN'}\n")
    if stopped:
        sys.stderr.write(f"not run ({ENDED_TEXT if ENDED else 'deadline'}): {len(stopped)}\n"
                         + "".join(f"  not run class: {r[0]}\n" for r in stopped))
    if ENDED:  # T-4399: the exit code says how the run ended (143 SIGTERM, 130 SIGINT, as the shell's own), never 124
        return 128 + ENDED[0]
    # T-2944: stopped at its own deadline with nothing really failed: exit 124 and say so last, so a caller (the
    # sentinel's re-verify) reads a busy machine, not a regression
    if stopped and not names:
        sys.stderr.write("run.py: stopped at its deadline\n")
        return 124
    return 1 if failed or cpu_red or not (total or skip or joined) else 0


def _end_from_outside(signum, frame):
    """T-4399: SIGTERM or SIGINT from outside (a shell's timeout, a Ctrl-C) ends the run the way --deadline does: the
    classes still running are killed, the ones not started are not run, and the report says so. The default handlers
    come back at once, so a second signal ends the process without waiting for the report."""
    ENDED.append(signum)
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, signal.SIG_DFL)


def _with_end_handlers(fn, a):
    """T-4399: fn(a) (a suite run) with the end handlers installed, and the callers' own handlers back when it returns:
    a run imported and called in-process keeps its signals."""
    old = {s: signal.signal(s, _end_from_outside) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        return fn(a)
    finally:
        for s, h in old.items():
            signal.signal(s, signal.SIG_DFL if h is None else h)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # T-1911: a crash after the notes was a bare exit 1 in CI
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=run.py::run.py crashed: {e!r}"[:1000], flush=True)
        raise
