"""fm exam's scratch store (T-3290, moved out of louhi.py): a project copied into a fresh temp folder, and the generation
arm's grow of K moons into it. louhi.py re-exports both names, so `sampo.louhi._scratch` is this module's object."""
import os
import shutil
import tempfile
import time

from sampo import core as c


def _scratch(p):
    """A project whose files live in a fresh temp folder (the real project's meta copied, so a sensitive project stays
    sensitive); the generation arm grows its moons there, never into the project's own store."""
    d = tempfile.mkdtemp(prefix="fm-bench-")
    shutil.copy(os.path.join(p.dir, "meta.json"), d)
    return c.Project(p.slug, p.root, d)


def _gen_grow(p, prof, k):
    """K moons grown by one profile with reasoning off into a scratch store, then the write-time admit's verdicts;
    {added, kept, seconds, fallback} (fallback: calls another model than the profile answered)."""
    from sampo import moonscale
    sp, was = _scratch(p), os.environ.get("MOONS_LOCAL")
    os.environ["MOONS_LOCAL"] = prof["name"]
    t0 = time.monotonic()
    try:
        r = moonscale.grow(sp, target=k, think=False)
    finally:
        os.environ.pop("MOONS_LOCAL", None) if was is None else os.environ.update(MOONS_LOCAL=was)
    mine = {prof["name"], prof["name"] + "+nothink"}
    return {"added": r["added"], "kept": r["kept"], "seconds": time.monotonic() - t0, "scratch": sp,
            "fallback": sum(n for m, n in r["models"].items() if m not in mine)}
