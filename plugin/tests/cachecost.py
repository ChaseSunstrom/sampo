"""T-5484: what the whole suite costs, read from the run cache (read only). A class whose newest record holds "*" reruns
on every change to the tree (its run read an unfollowed child, so no file list covers it). The report lists those
classes, costliest first, with the cause run.py kept for them (UNRECORDED for a record written before that), and the
suite's CPU s and tests with and without them.

    python3 plugin/tests/cachecost.py [CACHE_FILE]     (default: this repo's run cache)
"""
import json
import os
import subprocess
import sys

UNRECORDED = "unrecorded"
WHOLE = "*"


def _read(path):
    """({class id: {"tests", "paths", "cpu"}} for each class's newest record, {class id: cause}) from a run cache file;
    ({}, {}) when it is missing or unreadable. The newest record is the first (run.save_cache puts it first); cpu is
    None for a record written without a CPU reading."""
    try:
        with open(path) as f:
            d = json.load(f)
        table = d.get("paths", [])
        out = {}
        for c, vs in d.get("classes", {}).items():
            if not vs:
                continue
            v = vs[0]
            cpu = v[3][0] if len(v) > 3 and v[3] else None
            out[c] = {"tests": int(v[1]), "paths": [table[i] for i in v[2]], "cpu": cpu}
        why = d.get("why", {})
        return out, (why if isinstance(why, dict) else {})
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return {}, {}


def wholes(path):
    """The whole-tree classes, costliest first: [{"class", "tests", "cpu", "cause"}]."""
    classes, why = _read(path)
    rows = [{"class": c, "tests": r["tests"], "cpu": r["cpu"], "cause": str(why.get(c, UNRECORDED))}
            for c, r in classes.items() if WHOLE in r["paths"]]
    return sorted(rows, key=lambda row: (-(row["cpu"] or 0), row["class"]))


def summary(path):
    """The counts: classes, whole-tree classes, their tests and CPU s, and the same for all classes."""
    classes, _ = _read(path)
    whole = {c: r for c, r in classes.items() if WHOLE in r["paths"]}
    return {"classes": len(classes), "whole": len(whole),
            "tests": sum(r["tests"] for r in classes.values()), "whole_tests": sum(r["tests"] for r in whole.values()),
            "cpu": sum(r["cpu"] or 0 for r in classes.values()), "whole_cpu": sum(r["cpu"] or 0 for r in whole.values())}


def report(path, top=40):
    """Text: the counts, the whole-tree CPU by cause, then the costliest whole-tree classes."""
    s = summary(path)
    rows = wholes(path)
    lines = [f"{s['whole']} of {s['classes']} classes hold '*' (the whole tree): {s['whole_tests']} of {s['tests']} "
             f"tests, {s['whole_cpu']:.0f} of {s['cpu']:.0f} CPU s"]
    by = {}  # grouped by the cause's first word (its kind: git, browser, runs-python ...), the rest varies per run
    for row in rows:
        key = row["cause"].split(" ", 1)[0]
        n, cpu = by.get(key, (0, 0.0))
        by[key] = (n + 1, cpu + (row["cpu"] or 0))
    if by:
        lines.append("by cause:")
        for cause, (n, cpu) in sorted(by.items(), key=lambda kv: (-kv[1][1], kv[0])):
            lines.append(f"  {cpu:9.1f} s  {n:4d} classes  {cause}")
    lines.append("costliest:")
    for row in rows[:top]:
        lines.append(f"  {row['cpu'] or 0:9.1f} s  {row['tests']:4d} tests  {row['class']}  [{row['cause']}]")
    return "\n".join(lines) + "\n"


def default_cache():
    """This repo's run cache file, the one the suite writes (run.cache_path of the git common dir)."""
    import run  # the suite's own module, found beside this file
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True,
                            text=True, check=True, cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()
    return run.cache_path(os.path.realpath(common))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.stdout.write(report(sys.argv[1] if len(sys.argv) > 1 else default_cache()))
