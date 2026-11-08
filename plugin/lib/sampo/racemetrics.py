"""T-3280: a branch's import time, the evidence `fm lane race --score` prints per side. Evidence only: the ranking never reads it.

import_ms(path, files) imports each python module a branch changed under plugin/lib (plugin/lib/sampo/X.py is sampo.X,
plugin/lib/fmX.py is fmX), each import in a fresh interpreter, timed inside that interpreter so its start-up is left out.
Per module the median of RUNS runs; the modules' medians are summed, in whole ms, and the count of modules says what
the sum covers. It gives (ms, modules, why): ms is None, with why saying which module or limit, when a changed plugin/lib
.py was deleted, isn't a plain module name, fails to import or runs out of time, when more than MAX_MODULES changed, or
when none did: no honest number then.

The modules are the builder's own code and run unsandboxed, as the race's tests do: the environment is cut to PATH, a
temp HOME, LANG and PYTHONDONTWRITEBYTECODE, so no token or key of this shell is visible to them. Nothing stops one
reaching for the network or the disk by path. The tree is only read; nothing is written under it."""
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time

LIB = "plugin/lib/"
RUNS, RUN_SECS, TOTAL_SECS, MAX_MODULES = 3, 20, 60, 8  # per module, one run's timeout, the whole call, a race's cap
# ponytail: sequential runs on a loaded box; a parallel pool or a warm-up run if the timings prove too noisy.
_NAME = re.compile(r"[A-Za-z_]\w*(?:/[A-Za-z_]\w*)*\.py", re.ASCII)  # fullmatch: the name is passed as an argument, never shell
# the stdlib modules load before the tree is first on sys.path, so a module named time.py in the tree can't shadow them
_SCRIPT = ("import importlib, sys, time\n"
           "sys.path.insert(0, sys.argv[1])\n"
           "if sys.argv[2] in sys.modules: sys.exit(3)\n"  # loaded already (a stdlib name): cached, nothing measured
           "t = time.perf_counter()\n"
           "importlib.import_module(sys.argv[2])\n"
           "print((time.perf_counter() - t) * 1000)\n")


def module_of(rel):
    """The module name plugin/lib/<rel> imports as, or None when it isn't a plain python file under plugin/lib."""
    if not rel.startswith(LIB) or not _NAME.fullmatch(rel[len(LIB):]):
        return None
    name = rel[len(LIB):-len(".py")].replace("/", ".")
    return name[:-len(".__init__")] if name.endswith(".__init__") else name


def _once(root, name, cwd, env, secs):
    """One import's milliseconds in a fresh interpreter, or None (it failed, or timed out)."""
    try:
        got = subprocess.run([sys.executable, "-c", _SCRIPT, os.path.join(root, LIB.rstrip("/")), name], cwd=cwd,
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=secs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:  # the module may print anything (any bytes) before our last line
        ms = float(got.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return ms if got.returncode == 0 and math.isfinite(ms) else None


def import_ms(path, files):
    """(ms, modules, why) for the changed plugin/lib .py files in the tree at path (see the module docstring)."""
    lib = sorted({rel for rel in files if rel.startswith(LIB) and rel.endswith(".py")})
    if not lib:
        return None, 0, "no plugin/lib module changed"
    if len(lib) > MAX_MODULES:
        return None, len(lib), f"too many modules ({len(lib)}, at most {MAX_MODULES})"
    pairs = []
    for rel in lib:
        name = module_of(rel)
        if name is None:
            return None, len(lib), f"{rel} isn't a plain module name"
        if not os.path.isfile(os.path.join(path, rel)):
            return None, len(lib), f"{rel} is deleted"
        pairs.append((rel, name))
    deadline = time.monotonic() + TOTAL_SECS
    total = 0.0
    with tempfile.TemporaryDirectory(prefix="fm-imports-") as tmp:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "PYTHONDONTWRITEBYTECODE": "1",
               "LANG": os.environ.get("LANG", "C.UTF-8")}
        for rel, name in pairs:
            runs = []
            for _ in range(RUNS):
                left = deadline - time.monotonic()
                ms = _once(path, name, tmp, env, min(RUN_SECS, left)) if left > 0 else None
                if ms is None:
                    return None, len(lib), f"{rel} failed to import or ran out of time"
                runs.append(ms)
            total += statistics.median(runs)
    return round(total), len(lib), ""
