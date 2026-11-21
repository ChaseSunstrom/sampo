"""T-5016 (a slice of T-4322): `sampo wired` lists the shipped capabilities nothing reaches: each public function (and cmd_
handler) in plugin/lib/sampo that no verb and no hook leads to, as file:line rows. outcomes.census counts runs, so it can't see
a function no verb reaches; this is a static ast scan, and it never runs anything.

The graph. A node is one top-level def or class of a sampo module, or a module's own top-level statements (`<module>`: dispatch
tables, registries, decorators). A node's references are the names its body uses: a name of its own module's defs, a name a
`from sampo.x import f` bound, a module attribute (`x.f` for `from sampo import x`, `import sampo.x`, `sampo.x.f`), and a
string pair as in `lazy("sampo.x", "cmd_y")` (a Call whose first two arguments are a sampo module and a name). Referring to a
module reaches its `<module>` node (importing runs it). The seeds: cli.main, every statement of a hook or bin script and of
plugin/lib's other python files (fmguard), and a module's `if __name__ == "__main__":` block. Whatever the seeds reach is wired;
the public functions left over are the rows. Tests are never read: a function only a test calls is still a row.

A row says who refers to it: no caller at all, or only other unwired code (`called only by …`, the first few).

Known gaps, so a row is a lead and not a verdict: a call by getattr with a built name (a dispatch on "cmd_" + word), a method
called on an instance (a class counts as one node: its methods' references go with it once the class is named), and a function
reached only through another process (a script a verb writes and runs). A function wired that way shows as a row when it is
not; one a name table reaches by a bare string is never in the graph."""
import ast
import os
import re
import sys

from sampo import core as c

SRC = "plugin/lib/sampo"
MODULE = "<module>"
MAX_BYTES = 5_000_000   # a file bigger than this is not read
SHOW = 400              # rows the plain listing prints (--json has them all)
CALLERS = 3             # callers named on a row


def _read2(path):
    """(tree, reason): reason is "" when the file is simply absent, else why a present file was not read."""
    if os.path.islink(path):
        return None, "symlink"
    if not os.path.isfile(path):
        return None, ""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None, "too large"
        with open(path, encoding="utf-8", errors="replace") as fh:
            return ast.parse(fh.read(), path), ""
    except (SyntaxError, ValueError) as e:
        return None, f"syntax error ({type(e).__name__})"
    except (RecursionError, MemoryError, OSError) as e:
        return None, type(e).__name__


def _read(path):
    return _read2(path)[0]


# a python snippet in a string or shell script: `from sampo import x; x.f(`, `from sampo.x import f`, `sampo.x.f(`
_IMP_MOD = re.compile(r"from\s+sampo\s+import\s+(\w+)(?:\s+as\s+(\w+))?")
_IMP_NAME = re.compile(r"from\s+sampo\.(\w+)\s+import\s+(\w+)(?:\s+as\s+(\w+))?")
_DOTTED = re.compile(r"\bsampo\.(\w+)\.(\w+)")


def text_refs(g, text, dotted=True):
    """Nodes a python snippet inside a string or shell script refers to (regex, since it is not parsed)."""
    out = set()

    def hit(mod, name):
        if mod in g.mods:
            out.add((mod, MODULE))
            got = g.resolve(mod, name) if name else None
            if got:
                out.add(got)

    for mm in _IMP_MOD.finditer(text):
        mod, alias = mm.group(1), mm.group(2) or mm.group(1)
        hit(mod, None)
        for call in re.finditer(r"\b" + re.escape(alias) + r"\.(\w+)", text):
            hit(mod, call.group(1))
    for mm in _IMP_NAME.finditer(text):
        hit(mm.group(1), mm.group(2))
    for mm in _DOTTED.finditer(text) if dotted else ():
        hit(mm.group(1), mm.group(2))
    return out


def _stmts(body):
    """The module's statements with the bodies of its top-level if/try/with blocks opened (a def under `if X:` is still a def)."""
    for s in body:
        if isinstance(s, (ast.If, ast.Try, ast.With)) and not _main_block(s):
            for part in ("body", "orelse", "finalbody"):
                yield from _stmts(getattr(s, part, []))
            for h in getattr(s, "handlers", []):
                yield from _stmts(h.body)
        else:
            yield s


def _main_block(s):
    t = s.test if isinstance(s, ast.If) else None
    return (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__"
            and any(isinstance(x, ast.Constant) and x.value == "__main__" for x in t.comparators))


class Graph:
    def __init__(self, tree_of, names):
        self.trees = tree_of        # module name -> ast
        self.mods = set(names)      # sampo module names
        self.defs = {}              # (module, name) -> (line, node)
        self.top = {}               # module -> its non-def statements (the <module> node's body)
        self.mains = {}             # module -> statements under `if __name__ == "__main__":`
        self.mod_alias = {}         # file key -> {alias: dotted module}
        self.name_alias = {}        # file key -> {alias: (module, name)}
        for m, tree in tree_of.items():
            self._index(m, tree)

    def _module(self, dotted):
        """The sampo module a dotted name is, or None: `sampo.x` -> x."""
        if dotted.startswith("sampo.") and dotted[6:] in self.mods:
            return dotted[6:]
        return None

    def bind(self, key, tree):
        """Record the aliases the imports anywhere in this file bind."""
        mods, names = self.mod_alias.setdefault(key, {}), self.name_alias.setdefault(key, {})
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.asname:
                        mods[a.asname] = a.name
                    else:
                        mods[a.name.split(".")[0]] = a.name.split(".")[0]
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                for a in n.names:
                    dotted = f"{n.module}.{a.name}"
                    if self._module(dotted) or dotted == "sampo":
                        mods[a.asname or a.name] = dotted
                    elif self._module(n.module):
                        names[a.asname or a.name] = (self._module(n.module), a.name)

    def _index(self, m, tree):
        self.bind(m, tree)
        rest = []
        for s in _stmts(tree.body):
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[(m, s.name)] = (s.lineno, s)
            elif isinstance(s, ast.If) and _main_block(s):
                self.mains.setdefault(m, []).extend(s.body)
            else:
                rest.append(s)
        self.top[m] = rest

    def resolve(self, m, name, depth=0):
        """The def a name in module m's namespace is, following a re-export (`from sampo.core import X` in m)."""
        if (m, name) in self.defs:
            return (m, name)
        got = self.name_alias.get(m, {}).get(name)
        if got and depth < 4:
            return self.resolve(got[0], got[1], depth + 1)
        return None

    def refs(self, key, body):
        """The graph nodes the AST nodes `body` refer to, from the file `key`'s namespace (module m = key when it is one)."""
        out = set()
        mods, names = self.mod_alias.get(key, {}), self.name_alias.get(key, {})
        m = key if key in self.mods else None

        def module(dotted):
            mm = self._module(dotted)
            if mm:
                out.add((mm, MODULE))
            return mm

        for root in body:
            for n in ast.walk(root):
                if isinstance(n, ast.Name):
                    if m and (m, n.id) in self.defs:
                        out.add((m, n.id))
                    if n.id in names:
                        mm, nm = names[n.id]
                        out.add((mm, MODULE))
                        hit = self.resolve(mm, nm)
                        if hit:
                            out.add(hit)
                    if n.id in mods:
                        module(mods[n.id])
                elif isinstance(n, ast.Attribute):
                    parts, v = [n.attr], n.value
                    while isinstance(v, ast.Attribute):
                        parts.append(v.attr)
                        v = v.value
                    root_dotted = None
                    if isinstance(v, ast.Name) and v.id in mods:
                        root_dotted = mods[v.id]
                    elif (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "__import__"
                          and v.args and isinstance(v.args[0], ast.Constant) and isinstance(v.args[0].value, str)):
                        root_dotted = v.args[0].value.split(".")[0]  # __import__("sampo.x") returns `sampo`: .x.f follows
                    if root_dotted:
                        chain = root_dotted.split(".") + parts[::-1]  # sampo, x, f ...
                        for k in range(1, len(chain)):
                            mm = module(".".join(chain[:k]))
                            if mm:
                                hit = self.resolve(mm, chain[k])
                                if hit:
                                    out.add(hit)
                elif isinstance(n, ast.Constant):
                    if isinstance(n.value, str) and "from sampo" in n.value:
                        out |= text_refs(self, n.value, dotted=False)
                elif isinstance(n, ast.Call):
                    if (isinstance(n.func, ast.Name) and n.func.id == "getattr" and len(n.args) >= 2
                            and isinstance(n.args[0], ast.Name) and n.args[0].id in mods):
                        mm = module(mods[n.args[0].id])  # getattr(mod, built_name): any public def may be the one
                        if mm and not isinstance(n.args[1], ast.Constant):
                            out.update(k for k in self.defs if k[0] == mm and not k[1].startswith("_"))
                    lits = [a.value for a in n.args[:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                    if lits and len(n.args) >= 1 and isinstance(n.args[0], ast.Constant):
                        mm = module(lits[0])
                        if mm and len(lits) == 2:
                            hit = self.resolve(mm, lits[1])
                            if hit:
                                out.add(hit)
                        elif mm and len(n.args) > 1:  # lazy("sampo.instr", "cmd_" + name): every def with that prefix
                            b = n.args[1]
                            pre = b.left.value if (isinstance(b, ast.BinOp) and isinstance(b.op, ast.Add)
                                                   and isinstance(b.left, ast.Constant)
                                                   and isinstance(b.left.value, str)) else ""
                            if pre:
                                out.update(k for k in self.defs if k[0] == mm and k[1].startswith(pre))
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:  # importing runs the module, wired or not by name use
                        if isinstance(n, ast.Import):
                            module(a.name)
                        elif n.module and not n.level:
                            module(f"{n.module}.{a.name}") or module(n.module)
        return out

    def node_refs(self, key):
        m, name = key
        if name == MODULE:
            return self.refs(m, self.top.get(m, []))
        return self.refs(m, [self.defs[key][1]])


def _names(src):
    return sorted(f[:-3] for f in os.listdir(src) if f.endswith(".py") and f != "__init__.py"
                  and not os.path.isdir(os.path.join(src, f)))


def _not_python(path):
    """True for a script whose shebang names another interpreter (a sh entry beside the python hooks): not a parse failure."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" not in first


def _skip(skipped, path, plugin, reason, seed):
    rel = os.path.relpath(path, os.path.dirname(plugin)).replace(os.sep, "/")
    skipped.append({"file": rel, "reason": reason, "seed": seed})


def _scripts(plugin, skipped):
    """(key, tree) of each hook and bin script, each python file in plugin/lib outside the sampo package (fmguard; the fmX
    shims name a module and nothing else, so they only mark it reached), site/*.py beside plugin/, and plugin/templates python."""
    out = []
    root = os.path.dirname(plugin)

    def take(key, path, seed=True):
        if _not_python(path):
            return None
        tree, why = _read2(path)
        if tree is not None:
            out.append((key, tree))
        elif why:
            _skip(skipped, path, plugin, why, seed)
        return tree

    for folder in ("hooks", "bin"):
        d = os.path.join(plugin, folder)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else ():
            if "." not in f:
                take(f"{folder}/{f}", os.path.join(d, f))
    d = os.path.join(plugin, "lib")
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else ():
        if f.endswith(".py"):
            tree, why = _read2(os.path.join(d, f))
            if tree is None:
                if why:
                    _skip(skipped, os.path.join(d, f), plugin, why, True)
            elif not (ast.get_docstring(tree) or "").startswith("Old import path"):
                out.append((f"lib/{f}", tree))
    d = os.path.join(root, "site")
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else ():
        if f.endswith(".py"):
            take(f"site/{f}", os.path.join(d, f), seed=False)
    for dirpath, dirs, files in os.walk(os.path.join(plugin, "templates")):
        dirs.sort()
        for f in sorted(files):
            if f.endswith(".py"):
                take(f"templates/{os.path.relpath(os.path.join(dirpath, f), plugin)}", os.path.join(dirpath, f), seed=False)
    return out


def _shell_texts(plugin):
    """The text of each shell script at the repo root and in plugin/ (their `python3 -c '...'` strings seed the library)."""
    root = os.path.dirname(plugin)
    for d in (root, plugin):
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else ():
            p = os.path.join(d, f)
            if f.endswith(".sh") and os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) < MAX_BYTES:
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        yield fh.read()
                except OSError:
                    pass


def fatal(skipped):
    """True when a skipped file means the graph can't be trusted: cli.py, or any seed script."""
    return any(s["seed"] or s["file"].endswith("lib/sampo/cli.py") for s in skipped)


def scan(plugin):
    """The unwired public functions (see scan_full), rows only."""
    return scan_full(plugin)[0]


def scan_full(plugin):
    """(rows, skipped): the unwired public functions of plugin/lib/sampo under the plugin folder `plugin`, as rows sorted by
    file and line: {module, name, kind (cmd | function), file, line, callers (the unwired nodes that refer to it, as
    module.name)}; and the files that could not be read, {file, reason, seed}."""
    src = os.path.join(plugin, "lib", "sampo")
    skipped = []
    if not os.path.isdir(src):
        return [], skipped
    trees = {}
    for m in _names(src):
        tree, why = _read2(os.path.join(src, m + ".py"))
        if tree is not None:
            trees[m] = tree
        elif why:
            _skip(skipped, os.path.join(src, m + ".py"), plugin, why, False)
    g = Graph(trees, trees)
    seeds = set()
    if ("cli", "main") in g.defs:
        seeds.add(("cli", "main"))
    for key, tree in _scripts(plugin, skipped):
        g.bind(key, tree)
        seeds |= g.refs(key, tree.body)
    for text in _shell_texts(plugin):
        seeds |= text_refs(g, text)
    for m, body in g.mains.items():
        seeds |= g.refs(m, body)
    edges, seen, todo = {}, set(), list(seeds)
    while todo:
        k = todo.pop()
        if k in seen:
            continue
        seen.add(k)
        if k[1] == MODULE and k[0] not in g.top:
            continue
        edges[k] = g.node_refs(k)
        todo.extend(edges[k])
    callers = {}  # who refers to a node, among the nodes that are not reached
    for k in [(m, MODULE) for m in g.top] + list(g.defs):
        if k in seen:
            continue
        for t in g.node_refs(k):
            if t != k:
                callers.setdefault(t, set()).add(k)
    rows = []
    for (m, name), (line, node) in g.defs.items():
        if (m, name) in seen or name.startswith("_") or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        who = sorted(f"{a}.{b}" for a, b in callers.get((m, name), ()))
        rows.append({"module": m, "name": name, "kind": "cmd" if name.startswith("cmd_") else "function",
                     "file": f"{SRC}/{m}.py", "line": line, "callers": who})
    rows.sort(key=lambda r: (r["file"], r["line"]))
    return rows, skipped


def render(rows, shown=SHOW, skipped=()):
    """The plain listing: a header, then one `file:line name` row each."""
    lone = sum(1 for r in rows if not r["callers"])
    lines = [f"wired: {len(rows)} public functions in {SRC} no verb or hook reaches ({lone} with no caller at all)",
             "  not seen: a getattr with a built name on an instance, a script a verb writes and runs, a bare-string name table"]
    if skipped:
        lines.append(f"  skipped {len(skipped)} file{'s' if len(skipped) != 1 else ''}, so the graph is incomplete: "
                     + ", ".join(f"{x['file']} ({x['reason']})" for x in skipped[:CALLERS]) + (" …" if len(skipped) > CALLERS else ""))
    for r in rows[:shown]:
        note = (" (cmd handler)" if r["kind"] == "cmd" else "") + (
            f" called only by {', '.join(r['callers'][:CALLERS])}{' …' if len(r['callers']) > CALLERS else ''}"
            if r["callers"] else " no caller")
        lines.append(f"{r['file']}:{r['line']} {r['name']}{note}")
    if len(rows) > shown:
        lines.append(f"  … {len(rows) - shown} more (--json lists them all; --dead --module NAME narrow it)")
    return "\n".join(lines)


def cmd_wired(args):
    from sampo import cli
    rows, skipped = scan_full(c.PLUGIN_ROOT)
    mod = getattr(args, "module", None)
    if mod:
        mod = mod.removesuffix(".py")
        if not os.path.isfile(os.path.join(c.PLUGIN_ROOT, "lib", "sampo", mod + ".py")):
            # a typo read as "0 … no verb or hook reaches", the line a fully wired module gets
            print(f"sampo wired: no module {mod} in plugin/lib/sampo", file=sys.stderr)
            raise SystemExit(2)
        rows = [r for r in rows if r["module"] == mod]
    if getattr(args, "dead", False):
        rows = [r for r in rows if not r["callers"]]
    cli.out(args, {"count": len(rows), "rows": rows, "skipped": skipped}, c.plain_lines(render(rows, skipped=skipped)))
    if fatal(skipped):
        raise SystemExit(1)
