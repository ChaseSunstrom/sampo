"""T-3662: sampo panels: a typed JSON spec of panels drawn as SVG pictures of mechanisms (a funnel through a gate, an
orbit, tanks filling; T-3736's eight move, in SMIL loops: a state machine, swimlanes, a conveyor, a network, a chord ring,
a protocol stack, a circuit board, a constellation of modules; T-3737's eight more: a git metro map, a trace waterfall, a
treemap, log rain, a health radar, hourglass budgets, a decision tree, a circle pack) in the Board look, as dark and light
pairs. The drawing lives in plugin/cards/_lib/panelkit.py, stdlib only, so the 29 calling cards
(plugin/cards/panel-KIND) run the same code confined; this module is the project
side: it loads the kit, applies a repo's own colours, checks the gates and writes the files.

  sampo panels render SPEC.json [--style board|blueprint|paper] [--out DIR]   one NN-KIND-dark/light.svg pair per panel
  sampo panels sheet SPEC.json [--out STEM]                                    every panel in one STEM-dark/light.svg
  sampo panels sample [--style S]                                              a spec with one panel of every kind
  sampo panels plan [ROOT] [--out FILE]                                        a spec of a repo's real parts (T-3733)
  sampo panels kinds                                                           the kinds and what each draws"""
import importlib.util
import json
import os
import re
import sys
import threading

from sampo import core as c

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
KIT = os.path.join(ROOT, "plugin", "cards", "_lib", "panelkit.py")
OUT = os.path.join("docs", "assets", "panels")
_lock, _kit = threading.Lock(), []


def kit():
    """The panel kit module, loaded once by file path (it lives under the cards' _lib, which a card can read)."""
    with _lock:
        if not _kit:
            spec = importlib.util.spec_from_file_location("panelkit", KIT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _kit.append(mod)
    return _kit[0]


def load(path):
    """The spec, checked against the kit's types: ValueError lists what is wrong, each naming its field path."""
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"can't read {path}: {e}") from None
    if isinstance(spec, dict) and spec.get("panels") == []:
        raise ValueError(f"{path} has no panels: nothing to draw (sampo panels plan prints why a repo has none)")
    errs = kit().check_spec(spec)
    if errs:
        raise ValueError("; ".join(errs[:6]) + (f"; and {len(errs) - 6} more" if len(errs) > 6 else ""))
    return spec


def themes(root, style):
    """({dark, light} themes, source): the repo's own colours (DESIGN.md, tokens or CSS) when it has them, else the style."""
    from sampo import readme
    k, pal = kit(), readme.palette(root)
    if pal["source"] == "the Board look":
        return {s: k.theme(style, s) for s in ("dark", "light")}, f"the {style} style"
    return {s: k.theme(style, s, pal[s]) for s in ("dark", "light")}, pal["source"]


def _check(files):
    from sampo import works
    return [f"{os.path.basename(p)}: {g['gate']} {g['got']} ({g['bar']})" for p, text in files
            for g in works.inspect(text.encode(), "figure")["gates"] if g["ok"] is False]


def render(root, spec_path, out=None, style=None):
    """{files, snippets, source, failed}: NN-KIND-dark.svg and -light.svg per panel, written only after every one is drawn
    and the whole spec (and its words) passed."""
    from sampo import readme
    k, spec = kit(), load(spec_path)
    hits = readme.private_hits(json.dumps(spec, ensure_ascii=False))
    if hits:
        raise ValueError(f"the spec holds private text: {readme._said(hits)}")
    th, source = themes(root, style or spec.get("style") or "board")
    out = os.path.abspath(out or os.path.join(root, OUT))
    made = []
    for i, p in enumerate(spec["panels"], 1):
        stem = f"{i:02d}-{p['kind']}"
        for s in ("dark", "light"):
            made.append((os.path.join(out, f"{stem}-{s}.svg"), k.panel_svg(p, th[s], i, f"p{i}")))
    failed = _check(made) + [f"{os.path.basename(a)}: {b}" for a, t in made for b in k.check_svg(t)]
    if not failed:
        os.makedirs(out, exist_ok=True)
        for path, text in made:
            c.write_atomic(path, text)
    rel = lambda p: os.path.relpath(p, root).replace(os.sep, "/")  # noqa: E731
    snippets = [readme.picture(rel(made[j][0]), rel(made[j + 1][0]), f"{p['title']}: {k.KINDS[p['kind']]['brief']}")
                for j, p in zip(range(0, len(made), 2), spec["panels"])]
    return {"files": [p for p, _ in made], "snippets": snippets, "source": source, "failed": failed}


def sheet(root, spec_path, stem, style=None):
    """{files, source}: every panel in one grid svg, STEM-dark.svg and STEM-light.svg."""
    k, spec = kit(), load(spec_path)
    th, source = themes(root, style or spec.get("style") or "board")
    files = []
    for s in ("dark", "light"):
        path = os.path.abspath(f"{stem}-{s}.svg")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c.write_atomic(path, k.sheet_svg(spec["panels"], th[s]))
        files.append(path)
    return {"files": files, "source": source}


# ---------------------------------------------------------------- plan (T-3733)

# the kind that pictures each kind of part: never a guess, a part with no kind here has no panel
PART_KINDS = {"routes, commands, queues, stores and services, and how they connect": "arch",
              "jobs enqueued into a queue, counted by their call sites": "funnel",
              "CI jobs in file order, and scheduled jobs": "lanes",
              "store files: tables, models and structs, one opened": "filetree",
              "test files and their test counts": "terminal",
              "compose services, or a Dockerfile's build stages": "tanks"}
_BROKERS = ("redis", "rabbitmq", "kafka")
_DATABASES = ("postgres", "sqlite", "mysql", "mariadb", "mongodb", "elasticsearch")


def _fit(text, n):
    text = str(text).rstrip()
    return text if len(text) <= n else text[:n - 1] + "…"


def _tail(path, n=40):
    return path if len(path) <= n else "…" + path[-(n - 1):]


def _arch(found):
    """(panel or None, parts, the one label found when there is nothing to connect it to)."""
    nodes, edges, parts = [], [], []

    def node(nid, label, sub, rel):
        if not rel or len(nodes) >= 10:
            return None
        nodes.append({"id": nid, "label": _fit(label, 16), **({"sub": _fit(sub, 22)} if sub else {})})
        parts.append({"kind": "arch", "name": label, "file": rel, "what": sub or ""})
        return nid

    def edge(a, b, label=""):
        if a and b and a != b:
            edges.append({"from": a, "to": b, **({"label": _fit(label, 12)} if label else {})})
    app = node("app", os.path.basename(found["entry"][0]), "entry", found["entry"][0]) if found["entry"] else None
    routes = found["pages"] + found["api"]
    web = node("routes", "Routes", " ".join(routes[:3]), found["route_file"]) if routes else None  # not a list: a box caption, cut again to 22 characters by _fit
    edge(app, web, "serves")
    for i, cmd in enumerate(found["commands"][:3]):
        edge(app or web, node(f"cmd{i}", cmd["name"], _tail(cmd["file"], 22), cmd["file"]))
    top = web or app
    queue = work = None
    for q in found["queues"][:1]:
        queue = node("queue", q["name"], f"{len(q['jobs'])} jobs", q["file"])
        work = node("worker", os.path.basename(q["worker"]), "runs the jobs", q["worker"])
        edge(top, queue, "enqueues")
        edge(queue, work, "runs")
    store = None
    if found["stores"]:
        first = found["stores"][0]["file"]
        n = found["totals"]["stores"]
        store = node("store", os.path.basename(first), f"{n} store file{'s' * (n != 1)}", first)
        edge(top or work, store, "reads/writes")
    for i, dep in enumerate(d for d in found["deps"] if d["name"] != "celery"):
        d = node(f"dep{i}", dep["name"], "", dep["file"])
        src = (queue if dep["name"] in _BROKERS else store if dep["name"] in _DATABASES else None) or work or top
        edge(src, d)
    if len(nodes) < 2 or not edges:
        return None, [], [x["label"] for x in nodes]
    return ({"kind": "arch", "title": "How the parts connect", "stat": f"{len(nodes)} parts",
             "foot": "each box is a file or service found in the repo", "data": {"nodes": nodes, "edges": edges}}, parts, [])


def _funnels(found):
    out, skipped = [], []
    for q in found["queues"][:2]:
        jobs = [j for j in q["jobs"] if j["calls"] >= 1]
        if len(jobs) < 2:
            skipped.append({"part": q["name"], "file": q["file"], "kind": "funnel",
                            "why": "fewer than two jobs are enqueued anywhere: a funnel needs two sources"})
            continue
        jobs = sorted(jobs, key=lambda j: -j["calls"])[:8]
        runner = q["worker"] or q["file"]
        out.append(({"kind": "funnel", "title": _fit(f"Jobs into {q['name']}", 40),
                     "stat": f"{sum(j['calls'] for j in jobs)} call sites", "foot": "call sites that enqueue each job",
                     "data": {"sources": [{"label": _fit(j["name"], 18), "value": j["calls"]} for j in jobs],
                              "gate": _fit(q["name"], 14),
                              "result": {"label": _fit(os.path.basename(runner), 14), "sub": "runs the jobs"}}},
                    [{"kind": "funnel", "name": j["name"], "file": j["file"], "what": f"{j['calls']} call sites"}
                     for j in jobs] + [{"kind": "funnel", "name": q["name"], "file": runner, "what": "runs them"}]))
    return out, skipped


def _lanes(found):
    out, skipped = [], []
    for p in found["pipelines"][:2]:
        if len(p["stages"]) < 2:
            skipped.append({"part": p["name"], "file": p["file"], "kind": "lanes",
                            "why": "one job: lanes need two stages"})
            continue
        stages = [_fit(s, 10) for s in p["stages"][:7]]
        out.append(({"kind": "lanes", "title": _fit(f"Pipeline: {p['name']}", 40), "stat": f"{len(stages)} stages",
                     "foot": "the jobs in the order the file lists them",
                     "data": {"stages": stages, "lanes": [{"label": _fit(p["name"], 16), "at": len(stages) - 1}]}},
                    [{"kind": "lanes", "name": p["name"], "file": p["file"], "what": f"{len(stages)} stages"}]))
    names = list(dict.fromkeys(j["name"] for j in found["jobs"]))
    if len(names) >= 2:
        first = found["jobs"][0]["file"]
        stages = [_fit(n, 10) for n in names[:7]]
        out.append(({"kind": "lanes", "title": "Scheduled jobs", "stat": f"{found['totals']['jobs']} jobs",
                     "foot": "handlers registered with a scheduler",
                     "data": {"stages": stages, "lanes": [{"label": _fit(os.path.basename(first), 16), "at": len(stages) - 1}]}},
                    [{"kind": "lanes", "name": j["name"], "file": j["file"], "what": "scheduled"} for j in found["jobs"][:7]]))
    elif found["jobs"]:
        skipped.append({"part": found["jobs"][0]["name"], "file": found["jobs"][0]["file"], "kind": "lanes",
                        "why": "one scheduled job: lanes need two stages"})
    return out, skipped


def _tree(found):
    stores = found["stores"]
    if not stores:
        return None, [], []
    shown = next((s for s in stores if s["lines"]), None)
    if not shown:
        return None, [], [{"part": os.path.basename(stores[0]["file"]), "file": stores[0]["file"], "kind": "filetree",
                           "why": "no class, table or struct lines to open as a document"}]
    files = sorted(s["file"] for s in stores)[:16]
    total = found["totals"]["stores"]
    return ({"kind": "filetree", "title": "Where the data lives", "stat": f"{total} files",
             "foot": "files that define tables, models or structs" + (f", the first {len(stores)} shown" if total > len(stores) else ""),
             "data": {"files": [_tail(f) for f in files], "focus": _tail(shown["file"]),
                      "doc": {"title": _fit(os.path.basename(shown["file"]), 24),
                              "lines": [_fit(x, 34) for x in shown["lines"][:9]]}}},
            [{"kind": "filetree", "name": os.path.basename(s["file"]), "file": s["file"], "what": "store"} for s in stores],
            [])


def _terminal(found):
    tests = found["tests"]
    if not tests:
        return None, [], []
    rows = [[_fit(os.path.basename(t["file"]), 18), str(t["count"])] for t in tests[:8]]
    total, files = found["totals"]["test_count"], found["totals"]["tests"]
    return ({"kind": "terminal", "title": "Tests", "stat": f"{total} tests", "h": max(260, 142 + 24 * len(rows)),  # every row fits
             "foot": f"{files} test files, counted from the source" + (", the first 8 shown" if files > 8 else ""),
             "data": {"command": _fit(found["test_cmd"], 40), "columns": ["file", "tests"], "rows": rows}},
            [{"kind": "terminal", "name": os.path.basename(t["file"]), "file": t["file"], "what": f"{t['count']} tests"}
             for t in tests[:8]], [])


def _tanks(found):
    skipped = []
    for d in found["deploy"]:
        items = d["services"] if d["kind"] == "compose" else d["stages"]
        if len(items) < 2:
            if d["kind"] == "compose" or not any(x["kind"] == "compose" for x in found["deploy"]):
                skipped.append({"part": os.path.basename(d["file"]), "file": d["file"], "kind": "tanks",
                                "why": f"one {'compose service' if d['kind'] == 'compose' else 'build stage'} and no second "
                                       "to compare: no tanks to fill"})
            continue
        big = sorted(items, key=lambda x: -x["lines"])[:6]
        items = [x for x in items if x in big]
        top = max(x["lines"] for x in items)
        what = "compose services" if d["kind"] == "compose" else "build stages"
        return ({"kind": "tanks", "title": _fit(f"{what.capitalize()}", 40), "stat": f"{len(items)} {what.split()[1]}",
                 "foot": "fill: lines of config each takes, a size and not a load",
                 "data": {"tanks": [{"label": _fit(x["name"], 12), "value": round(100 * x["lines"] / top) if top else 0}
                                    for x in items]}},
                [{"kind": "tanks", "name": x["name"], "file": d["file"], "what": f"{x['lines']} lines"} for x in items], skipped)
    return None, [], skipped


def _empty(name, reason, parts=(), skipped=()):
    # the reason is beside the spec, not in it: the kit's spec has no such field (and wants a panel, so render says "no panels")
    return {"spec": {"name": _fit(name, 40), "panels": []}, "parts": list(parts),
            "skipped": list(skipped), "reason": reason}


def plan(root):
    """{spec, parts, skipped, reason}: a panel spec (what sampo panels render takes) of a repo's real parts, one kind of
    panel per kind of part (PART_KINDS). parts name the file each box or row was read from, skipped what was found and
    not drawn, and why. A repo with nothing to draw is an empty-panels spec with a reason: no part is ever invented."""
    from sampo import readme
    root = os.path.abspath(root)
    name = os.path.basename(root.rstrip("/")) or "project"
    if not os.path.isdir(root):
        return _empty(name, f"{root} is not a folder")
    found = readme.scan(root, parts=True)
    built, skipped = [], []
    arch, aparts, lone = _arch(found)
    if arch:
        built.append((arch, aparts))
    fun, left = _funnels(found)
    built += fun
    skipped += left
    lanes, left = _lanes(found)
    built += lanes
    skipped += left
    for make in (_tree, _terminal, _tanks):
        panel, parts, left = make(found)
        if panel:
            built.append((panel, parts))
        skipped += left
    spec, parts, kept = {"name": _fit(name, 40), "title": _fit(f"{name}: its real parts", 80), "panels": []}, [], []
    for panel, got in built:  # the net: a panel the kit refuses, or one that holds private text, is left out and said
        errs = kit().check_spec({"panels": [panel]})
        hits = readme.private_hits(json.dumps([panel, got], ensure_ascii=False))
        if errs or hits:
            skipped.append({"part": panel["title"], "file": got[0]["file"] if got and not hits else "", "kind": panel["kind"],
                            "why": errs[0] if errs else f"holds private text: {readme._said(hits)}"})
            continue
        spec["panels"].append(panel)
        parts += got
    if not spec["panels"]:
        if lone:
            why = f"found only {', '.join(lone)}: one part has nothing to connect to, so nothing is drawn"
        elif skipped:
            why = "found parts, but none can be drawn: " + "; ".join(f"{s['part']}: {s['why']}" for s in skipped)[:400]
        else:
            why = ("no services, routes, queues, jobs, stores, pipelines, tests or deploy files found: nothing to draw "
                   "that isn't invented")
        return _empty(name, why, skipped=skipped)
    return {"spec": spec, "parts": parts, "skipped": skipped, "reason": ""}


def cmd_plan(args, root):
    """sampo panels plan [ROOT] [--out FILE] [--json]: the spec on stdout (or in FILE), what was left out on stderr."""
    from sampo import cli
    res = plan(root)
    text = json.dumps(res["spec"], indent=2, ensure_ascii=False)
    if args.out:
        c.write_atomic(os.path.abspath(args.out), text + "\n")
    notes = [f"sampo panels plan: {len(res['spec']['panels'])} panels from {len(res['parts'])} parts in {root}"]
    notes += [f"  left out: {s['part']} ({s['file']}): {s['why']}" for s in res["skipped"]]
    notes += [f"  {res['reason']}"] if res["reason"] else []
    notes += ["  render it: sampo panels render " + (args.out or "SPEC.json")] if res["parts"] else []
    if getattr(args, "json", False):
        cli.out(args, res, None)
    elif args.out:
        print("\n".join(notes))
        return None
    else:
        print(text)
    print("\n".join(notes), file=sys.stderr)
    return None


def cmd_panels(args):
    from sampo import cli, readme
    k, root = kit(), os.path.abspath(args.root) if args.root else readme.repo_root()
    if args.action == "plan":
        return cmd_plan(args, os.path.abspath(args.target) if args.target else root)
    if args.action == "kinds":
        w = max(map(len, k.KINDS))
        cli.out(args, {n: v["brief"] for n, v in k.KINDS.items()},
                "\n".join(f"{n:<{w}}  {v['brief']}" for n, v in k.KINDS.items()))
        return None
    if args.action == "sample":
        text = json.dumps(k.sample_spec(args.style or "board"), indent=2, ensure_ascii=False)
        cli.out(args, json.loads(text), text)
        return None
    if not args.target:
        raise cli.UsageError(f"sampo panels {args.action} SPEC.json: {{name, style, panels: [{{kind, title, stat, foot, w, h, "
                             "data}]}}; sampo panels sample prints one")
    try:
        if args.action == "sheet":
            res = sheet(root, os.path.abspath(args.target), args.out or "panels-sheet", args.style)
            cli.out(args, res, f"sampo panels sheet: drawn in {res['source']}:\n" + "\n".join(f"  {p}" for p in res["files"]))
            return None
        from sampo import panelhtml
        if panelhtml.dispatch(args, root):  # T-3734: --html (an animated page) and --term (drawn in the terminal)
            return None
        res = render(root, os.path.abspath(args.target), args.out, args.style)
    except ValueError as e:
        raise cli.UsageError(f"sampo panels {args.action}: {e}") from None
    text = "\n".join([f"sampo panels render: {len(res['files'])} files, drawn in {res['source']}:"]
                     + [f"  {os.path.relpath(f, root)}" for f in res["files"]] + [f"  ✗ {x}" for x in res["failed"]]
                     + ["", "Paste into the README:", ""] + res["snippets"])
    cli.out(args, res, text)
    if res["failed"]:
        raise SystemExit(1)
    return None
