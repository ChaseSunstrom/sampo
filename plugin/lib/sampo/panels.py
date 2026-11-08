"""T-3662: sampo panels: a typed JSON spec of panels drawn as static SVG pictures of mechanisms (a funnel through a gate,
an orbit, tanks filling) in the Board look, as dark and light pairs. The drawing lives in plugin/cards/_lib/panelkit.py,
stdlib only, so the 13 calling cards (plugin/cards/panel-KIND) run the same code confined; this module is the project
side: it loads the kit, applies a repo's own colours, checks the gates and writes the files.

  sampo panels render SPEC.json [--style board|blueprint|paper] [--out DIR]   one NN-KIND-dark/light.svg pair per panel
  sampo panels sheet SPEC.json [--out STEM]                                    every panel in one STEM-dark/light.svg
  sampo panels sample [--style S]                                              a spec with one panel of every kind
  sampo panels kinds                                                           the kinds and what each draws"""
import importlib.util
import json
import os
import re
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


def cmd_panels(args):
    from sampo import cli, readme
    k, root = kit(), os.path.abspath(args.root) if args.root else readme.repo_root()
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
