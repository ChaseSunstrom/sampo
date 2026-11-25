"""T-5288 (child of T-4333, Inventories as tests): every request that can leave the machine with text in it is routed
through redact_local, or on the reviewed list below with a kind and a reason. An AST walk over plugin/lib finds the sinks:
a urllib Request that carries data or a built URL; a urlopen that takes data or a URL that is not a constant and not a
Request the same function built; a gh or glab argv, run directly or through a wrapper (a function that forwards to gh,
found by its own ["gh", *args] list, and any function that forwards to such a wrapper), whose subcommand is a write
(create, comment, edit, review, close, reopen, merge, ready, delete, upload, transfer, rename, lock, unlock), or an api
call with a write flag (-f -F --field --raw-field -X --method), or whose argv is not spelled out (a list passed by
name); and a Jira or Linear post through eco._tracker (a post= argument, by keyword or third position).

A sink is routed when redact_local is called on its arguments (not under a branch), or on a name it takes that the same
function assigns from a redact_local call at the function's top level above it, with no parameter sent raw. What the walk does not see: a value that leaves its function through a return and
is sent by a caller (the caller is the sink, and is reviewed by hand); a request sent through an opener the walk cannot
name; a subprocess spelled other than as gh or glab. The list is a review aid, not a proof."""
import ast
import functools
import os
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import pr

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
# A word anywhere in a gh argv that names a write (gh -R repo issue create is a write too). Glab's verbs included.
WRITE_SUBS = frozenset(("create", "comment", "edit", "review", "close", "reopen", "merge", "ready", "delete", "upload",
                        "transfer", "rename", "lock", "unlock", "note", "update", "accept", "approve", "archive",
                        "unarchive", "add", "set", "remove", "item-create", "item-add", "item-edit", "item-delete",
                        "item-archive"))
# An api flag that sends a body or a method, glued to its value or not: -fbody=x, -XPOST, --method=POST, --input f.
API_WRITE = ("-f", "-F", "-X", "--field", "--raw-field", "--method", "--input")
KINDS = ("fixed", "model", "routed")  # fixed: no free text (a fixed route, a number, a key, a public name, a read-only query);
# model: a model server's transport (the prompt is the caller's; see the reason); routed: a caller redacts the text first

# {(file under plugin/lib, qualified function): (kind, reason, [sink labels])} for each unrouted sink the walk finds. A routed
# sink needs no entry; a reviewed entry covers exactly the labels it lists. Reasons carry placeholders only, never a real host, path or name.
REVIEWED = {
    ("sampo/bench_swe.py", "fetch_rows"): ("fixed", "pages the public benchmark's rows: a dataset name Sampo keeps and an offset; no text of the user's leaves", ['urlopen to a built URL']),
    ("sampo/ci.py", "_api"): ("fixed", "GitHub REST GET of this repo's runs and checks: the repo name from its git remote and a fixed route; a sensitive project or FOREMAN_NO_NET never reaches it", ['urllib Request to a built URL']),
    ("sampo/ci.py", "_latest"): ("fixed", "gh run list: fixed words and the branch name, a read", ['gh argv passed by name']),
    ("sampo/concerns.py", "_url_answers"): ("fixed", "GET of a URL the concerns list names, no body and no query of ours; reads a status", ['urllib Request to a built URL']),
    ("sampo/deps.py", "_get"): ("fixed", "GET of a public package registry's JSON for a dependency name from the project's manifest; https, redirects re-checked", ['urllib Request to a built URL']),
    ("sampo/eco.py", "_tracker"): ("routed", "the Jira or Linear lookup of a ticket key (a read), and the comment or GraphQL post whose text eco._post redacts first, its only posting caller", ['urllib Request to a built URL', 'urllib Request with data', 'urllib Request with data']),
    ("sampo/eco.py", "_gh_json"): ("fixed", "the read-only gh helper (its docstring): the argv is the caller's read list", ['gh argv passed by name']),
    ("sampo/eco.py", "_post.ask"): ("routed", "Linear's issue lookup (a fixed query) and the commentCreate whose body eco._post has redacted", ['Jira or Linear post']),
    ("sampo/foresight.py", "ci_main"): ("fixed", "GitHub Actions run list for a branch: the repo from git's remote, the branch percent-quoted; a read", ['urllib Request to a built URL']),
    ("sampo/live.py", "_get_json"): ("fixed", "a loopback (127.0.0.1) status GET; the local server's own URL, never leaves the machine", ['urlopen to a built URL']),
    ("sampo/models.py", "_local_context.ask"): ("model", "a model server's context-size metadata call (a model name from config, to a loopback or the user's own server); no prompt text", ['urllib Request with data']),
    ("sampo/models.py", "served_context"): ("model", "Ollama's /api/show with a configured model name, to the user's own server, for its context size; no prompt text", ['urllib Request with data']),
    ("sampo/models.py", "_send"): ("model", "model transport: the body is the caller's prompt for the endpoint the user configured (a local server or their own key); redacting a prompt belongs to the code that builds it, not to this transport", ['urllib Request with data']),
    ("sampo/moonscale.py", "_up"): ("model", "GET of the user's model server's /models list: the base URL from the model profile, the key in a header", ['urllib Request to a built URL']),
    ("sampo/outside.py", "latest"): ("fixed", "GET of a registry's latest release for a dependency name; public https only (research.public_https)", ['urllib Request to a built URL']),
    ("sampo/pr.py", "threads"): ("fixed", "GraphQL read of a PR's review threads: a constant query and the owner and name GitHub gave", ['gh api write -F -f']),
    ("sampo/pr.py", "train"): ("fixed", "merge by PR number, pinned to the tested head sha; carries no text", ['gh write merge --match-head-commit --merge']),
    ("sampo/readme.py", "_answers"): ("fixed", "a GET of a link the project's own docs publish, to see that it answers; the link is the only thing sent", ['urlopen to a built URL']),
    ("sampo/research.py", "fetch"): ("fixed", "GET of a public https page a research source names (public_https checked on each hop); the URL is the only thing sent", ['urllib Request to a built URL']),
    ("sampo/smoke.py", "_other_checks"): ("fixed", "GET of the project's own smoke-check URL from its config", ['urlopen to a built URL']),
}


def _name(fn, alias):
    n = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
    return alias.get(n, n)


def _given(e):
    return e is not None and not (isinstance(e, ast.Constant) and e.value is None)


def _words(args):
    """The constant words of an argv: plain strings, and the strings of list or tuple literals and of a + chain."""
    out = []

    def take(e):
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            out.append(e.value)
        elif isinstance(e, (ast.List, ast.Tuple)):
            for x in e.elts:
                take(x)
        elif isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
            take(e.left)
            take(e.right)
    for a in args:
        take(a)
    return out


def _is_gh(e):
    """The program of an argv: gh or glab, by name or by path (/usr/bin/gh)."""
    return isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value.rsplit("/", 1)[-1] in ("gh", "glab")


def _argv(n):
    """A list or tuple literal that spells a gh or glab argv first."""
    return isinstance(n, (ast.List, ast.Tuple)) and bool(n.elts) and _is_gh(n.elts[0])


def _dynamic(args):
    """An argv is by name when it spells no words at all, or passes a starred list (its words are not all here)."""
    return not _words(args) or any(isinstance(a, ast.Starred) for a in args)


def _gh_sink(words, dynamic):
    """What a gh argv is, by its words: a write word anywhere in it, an api call with a write flag, or (for a call that
    passes its argv by name) nothing spelled out at all. '' for a read."""
    writes = sorted({w for w in words if w in WRITE_SUBS})
    if writes:
        return "gh write " + " ".join(writes)
    if "api" in words[:3] and any(w.startswith(f) for w in words for f in API_WRITE):
        return "gh api write"
    if dynamic:
        return "gh argv passed by name"
    return ""


def _gh_label(kind, words):
    """A gh sink's label: its kind and every flag its argv spells, so a flag added to a reviewed call (a --body on a merge)
    changes the label and the call is open again."""
    flags = sorted({w.split("=", 1)[0] for w in words if w.startswith("-")})
    return " ".join([kind] + flags)


def _parents(tree):
    return {ch: n for n in ast.walk(tree) for ch in ast.iter_child_nodes(n)}


def _enclosing(node, parents, kinds=(ast.FunctionDef, ast.AsyncFunctionDef)):
    while node in parents:
        node = parents[node]
        if isinstance(node, kinds):
            return node
    return None


def _qual(node, parents):
    out, n = [], node
    while n in parents:
        n = parents[n]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(n.name)
    return ".".join(reversed(out)) or "<module>"


def _wrappers(trees):
    """{(file, name): seed}: the functions that send an argv to gh or glab. A seed spells ["gh", *args] (ci._gh, eco._gh,
    board._gh). A forwarder hands its own parameter on to a seed, one hop (eco._gh_json -> eco._gh). A call that passes its
    argv by name is judged where a seed is called; a forwarder's own call is judged in its body, so its callers aren't."""
    seeds, forwards = set(), []
    for rel, tree in trees.items():
        alias, parents = _alias(tree), _parents(tree)
        for n in ast.walk(tree):
            if _argv(n) and any(isinstance(e, ast.Starred) for e in n.elts):
                fn = _enclosing(n, parents)
                if fn is not None:
                    seeds.add((rel, fn.name))
            if isinstance(n, ast.Call) and _dynamic(n.args):
                fn = _enclosing(n, parents)
                if fn is None:
                    continue
                params = {a.arg for a in fn.args.args + fn.args.kwonlyargs} | ({fn.args.vararg.arg} if fn.args.vararg else set())
                if any(_param_arg(a, params) for a in n.args):
                    forwards.append((rel, fn.name, n.func, alias))
    out, names = {k: True for k in seeds}, {rel: _file_names(tree) for rel, tree in trees.items()}
    for rel, fname, func, alias in forwards:
        if _wrapper_of(rel, names[rel], func, alias, seeds) and (rel, fname) not in out:
            out[(rel, fname)] = False
    return out


def _wrapper_of(rel, names, func, alias, wrappers):
    """The wrapper a call to `func` in this file reaches: a module attribute (ci._gh) by its name, or a plain name by
    _resolve. None when the call reaches none of them."""
    if isinstance(func, ast.Attribute):
        return next((k for k in wrappers if k[1] == func.attr), None)
    return _resolve(rel, names, _name(func, alias), wrappers)


def _file_names(tree):
    """(names this file defines as functions, names it imports)."""
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    return defined, imported


def _resolve(rel, names, name, wrappers):
    """The wrapper a call of `name` in this file reaches: the file's own def of it, or a name the file imports; else None."""
    if (rel, name) in wrappers:
        return (rel, name)
    defined, imported = names
    if name in imported and name not in defined:
        return next((k for k in wrappers if k[1] == name), None)
    return None


def _param_arg(a, params):
    x = a.value if isinstance(a, ast.Starred) else a
    return isinstance(x, ast.Name) and x.id in params


def _alias(tree):
    return {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names if a.asname}


def _request_built(url, call, parents, alias, tree):
    """A name that the enclosing function (or the module) assigns from a Request(...) call."""
    if not isinstance(url, ast.Name):
        return False
    scope = _enclosing(call, parents) or tree
    for x in ast.walk(scope):
        if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == url.id for t in x.targets):
            if isinstance(x.value, ast.Call) and _name(x.value.func, alias) == "Request":
                return True
    return False


def _sinks(rel, tree, wrappers):
    """(call or list node, what it sends) for each outside request the file makes."""
    alias, parents, out, names = _alias(tree), _parents(tree), [], _file_names(tree)
    for n in ast.walk(tree):
        if _argv(n):
            words = _words(n.elts[1:])
            kind = _gh_sink(words, dynamic=False)
            if kind:
                out.append((n, _gh_label(kind, words)))
        if not isinstance(n, ast.Call):
            continue
        name = _name(n.func, alias)
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        if name == "Request":
            url = n.args[0] if n.args else kw.get("url")
            data = n.args[1] if len(n.args) > 1 else kw.get("data")
            if _given(data):
                out.append((n, "urllib Request with data"))
            elif url is not None and not isinstance(url, ast.Constant):
                out.append((n, "urllib Request to a built URL"))
        elif name == "urlopen":
            url = n.args[0] if n.args else kw.get("url")
            data = n.args[1] if len(n.args) > 1 else kw.get("data")
            if _given(data):
                out.append((n, "urlopen with data"))
            elif url is not None and not isinstance(url, ast.Constant) and not _request_built(url, n, parents, alias, tree):
                out.append((n, "urlopen to a built URL"))
        elif name == "_tracker":
            post = n.args[2] if len(n.args) > 2 else kw.get("post")
            if _given(post):
                out.append((n, "Jira or Linear post"))
        elif _wrapper_of(rel, names, n.func, alias, wrappers):
            words = _words(n.args)
            kind = _gh_sink(words, dynamic=_dynamic(n.args) and wrappers[_wrapper_of(rel, names, n.func, alias, wrappers)])
            if kind:
                out.append((n, _gh_label(kind, words)))
    return out, alias, parents


def _under_choice(node, root, parents):
    """True when node sits under a conditional or a short-circuit between it and root (the argument it is part of)."""
    n = node
    while n is not root and n in parents:
        n = parents[n]
        if isinstance(n, (ast.IfExp, ast.BoolOp, ast.Lambda)):
            return True
    return False


def _redacts(e, alias):
    return any(isinstance(y, ast.Call) and _name(y.func, alias) == "redact_local" for y in ast.walk(e))


def _text_args(call, alias, context):
    """The arguments that carry a sink's text. A gh wrapper's first positional is its project handle (_gh(p, ...)), not
    text, and a tracker post's text is its third argument (the service and the key are context)."""
    if isinstance(call, (ast.List, ast.Tuple)):
        return list(call.elts)
    if _name(call.func, alias) == "_tracker":
        post = call.args[2] if len(call.args) > 2 else next((k.value for k in call.keywords if k.arg == "post"), None)
        return [] if post is None else [post]
    args = list(call.args[1:] if context else call.args)
    return args + [k.value for k in call.keywords]


def _routed(call, alias, parents, tree, context=False):
    """Routed when a redact_local call, not under a branch, is one of the call's text arguments; or when a name the call
    sends that the function builds has its assigns all at the function's top level, before the statement that makes the
    call, and its last one redacts (a redaction under an if routes only some paths, so it doesn't count), and no parameter
    the call sends goes out without such a redaction. A name that is neither built nor a parameter (a PR number) is
    ignored. context: the call is a gh wrapper, whose first positional argument is the project handle, not text."""
    names = set()
    parts = _text_args(call, alias, context)
    for a in parts:
        for x in ast.walk(a):
            if isinstance(x, ast.Call) and _name(x.func, alias) == "redact_local" and not _under_choice(x, a, parents):
                return True
            if isinstance(x, ast.Name):
                names.add(x.id)
    scope = _enclosing(call, parents) or tree
    top = call
    while top in parents and parents[top] is not scope:
        top = parents[top]
    body = scope.body
    assigns = {}
    for x in ast.walk(scope):
        if isinstance(x, ast.Assign):
            for t in x.targets:
                if isinstance(t, ast.Name):
                    assigns.setdefault(t.id, []).append(x)
    if top not in body:
        return False
    params, routed, raw_param = _params(scope), False, False
    for n in names:
        if n not in assigns and n not in params:
            continue  # a global or a constant the call names, not text this function built
        sts = assigns.get(n, [])
        if sts and not any(s not in body or s.lineno >= top.lineno for s in sts) and _redacts(
                max(sts, key=lambda s: s.lineno).value, alias):
            routed = True
        elif n in params:
            raw_param = True  # a parameter sent without a redaction that dominates the call
    return routed and not raw_param


def _params(scope):
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {a.arg for a in scope.args.args + scope.args.kwonlyargs}
    return set()


def _audit(trees, reviewed):
    """(sites that are unrouted and unreviewed or whose sink labels differ from the reviewed ones, reviewed entries with no
    unrouted site, entries with a bad kind, every unrouted site by (file, function)). trees: {file under plugin/lib: ast}.
    A reviewed entry covers the labels it lists, not the whole function: a new sink in it is open."""
    wrappers = _wrappers(trees)
    sites, labels = {}, {}
    for rel, tree in sorted(trees.items()):
        found, alias, parents = _sinks(rel, tree, wrappers)
        for node, what in found:
            if _routed(node, alias, parents, tree, context=what.startswith("gh ") and isinstance(node, ast.Call)):
                continue
            key = (rel, _qual(node, parents))
            sites.setdefault(key, []).append(f"{rel}:{node.lineno} {what}")
            labels.setdefault(key, []).append(what)
    open_ = sorted(k for k in sites if k not in reviewed or sorted(labels[k]) != sorted(reviewed[k][2]))
    stale = sorted(k for k in reviewed if k not in sites)
    bad = sorted(k for k, (kind, why, _) in reviewed.items() if kind not in KINDS or not why.strip())
    return open_, stale, bad, sites


def _load_lib():
    trees = {}
    for root, _, names in os.walk(LIB):
        for name in names:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    trees[os.path.relpath(path, LIB).replace(os.sep, "/")] = ast.parse(fh.read(), path)
    return trees


class OutwardSinkInventory(unittest.TestCase):
    def test_every_outward_sink_is_routed_or_reviewed(self):
        open_, _, _, sites = _audit(_load_lib(), REVIEWED)
        self.assertEqual(open_, [], "route each through redact_local, or review it with a kind and a reason: "
                         + "; ".join(f"{k[0]} {k[1]}: {sites[k]}" for k in open_))

    def test_a_reviewed_entry_is_still_an_unrouted_sink(self):
        _, stale, _, _ = _audit(_load_lib(), REVIEWED)
        self.assertEqual(stale, [], "reviewed, but no longer an unrouted sink: drop it from the list")

    def test_each_entry_names_a_kind_and_a_reason(self):
        _, _, bad, _ = _audit(_load_lib(), REVIEWED)
        self.assertEqual(bad, [], f"kind is one of {KINDS}, and the reason is not empty")

    def test_an_unrouted_post_fails(self):
        src = {"s.py": ast.parse("import urllib.request\n\ndef send(body):\n    urllib.request.urlopen(\n"
                                 "        urllib.request.Request('https://x.example', data=body))\n")}
        open_, _, _, _ = _audit(src, {})
        self.assertEqual(open_, [("s.py", "send")], open_)
        routed = ast.parse("import urllib.request\nfrom sampo.core import redact_local as rl\n\ndef send(body):\n"
                           "    body = rl(body)\n    urllib.request.urlopen(urllib.request.Request('https://x.example', "
                           "data=body))\n")
        self.assertEqual(_audit({"s.py": routed}, {})[0], [], "a name redacted above the sink is routed")

    def test_a_gh_issue_create_through_a_wrapper_fails(self):
        wrapper = ast.parse("import subprocess\n\ndef _gh(p, *argv):\n    return subprocess.run(['gh', *argv])\n\n"
                            "def _latest(p):\n    return _gh(p, 'run', 'list')\n")
        caller = ast.parse("from sampo.ci import _gh\n\ndef report(p, title):\n    _gh(p, 'issue', 'create', '--title', title)\n")
        open_, _, _, _ = _audit({"ci.py": wrapper, "r.py": caller}, {})
        self.assertEqual(open_, [("r.py", "report")], open_)
        read = ast.parse("from sampo.ci import _gh\n\ndef look(p):\n    return _gh(p, 'pr', 'view', '--json', 'number')\n")
        self.assertEqual(_audit({"ci.py": wrapper, "r.py": read}, {})[0], [], "a read through the wrapper is not a sink")

    def test_a_gh_write_through_a_forwarding_helper_fails(self):
        wrapper = ast.parse("import subprocess\n\ndef _gh(p, *argv):\n    return subprocess.run(['gh', *argv])\n\n"
                            "def _gh_json(p, args):\n    return _gh(p, args)\n")
        caller = ast.parse("from eco import _gh_json\n\ndef file(p, title):\n    return _gh_json(p, ['issue', 'create', title])\n")
        open_, _, _, _ = _audit({"eco.py": wrapper, "r.py": caller}, {})
        self.assertIn(("r.py", "file"), open_, open_)  # the helper's own by-name call is open too (its body is reviewed)

    def test_a_jira_or_linear_post_through_the_tracker_fails(self):
        src = ast.parse("def ping(svc, key, body):\n    return _tracker(svc, key, body)\n")
        open_, _, _, _ = _audit({"e.py": src}, {})
        self.assertEqual(open_, [("e.py", "ping")], open_)
        self.assertEqual(_audit({"e.py": ast.parse("def look(svc, key):\n    return _tracker(svc, key)\n")}, {})[0], [])

    def test_a_stale_entry_fails(self):
        clean = {"c.py": ast.parse("def f():\n    return 1\n")}
        _, stale, _, _ = _audit(clean, {("c.py", "gone"): ("fixed", "was a read", [])})
        self.assertEqual(stale, [("c.py", "gone")])
        open_, _, _, _ = _audit({"s.py": ast.parse("import urllib.request\n\ndef send(body):\n    urllib.request.urlopen(urllib.request.Request('https://x', data=body))\n")},
                                {("s.py", "send"): ("fixed", "reviewed as a read", ["urlopen to a built URL", "urllib Request with data"])})
        self.assertEqual(open_, [], "a reviewed unrouted sink passes")

    def test_a_new_sink_in_a_reviewed_function_fails(self):
        # the review covers the sinks it listed, not the whole function: a comment added to a reviewed merge is open
        wrap = "import subprocess\n\ndef _gh(p, *args):\n    return subprocess.run(['gh', *args])\n"
        before = ("from sampo.ci import _gh\n\ndef train(p, n, head):\n"
                  "    _gh(p, 'pr', 'merge', str(n), '--merge', '--match-head-commit', head)\n")
        after = before + "    _gh(p, 'pr', 'comment', str(n), '--body', 'text')\n"
        reviewed = {("p.py", "train"): ("fixed", "merge by PR number; carries no text", ["gh write merge --match-head-commit --merge"])}
        self.assertEqual(_audit({"ci.py": ast.parse(wrap), "p.py": ast.parse(before)}, reviewed)[0], [], "the review as written passes")
        self.assertEqual(_audit({"ci.py": ast.parse(wrap), "p.py": ast.parse(after)}, reviewed)[0], [("p.py", "train")],
                         "a new sink in the reviewed function is open")

    def test_a_flag_added_to_a_reviewed_call_fails(self):
        # a --body on the reviewed merge is text leaving the reviewed call: its label changes, so it is open
        wrap = "import subprocess\n\ndef _gh(p, *args):\n    return subprocess.run(['gh', *args])\n"
        src = ("from sampo.ci import _gh\n\ndef train(p, n, head):\n"
               "    _gh(p, 'pr', 'merge', str(n), '--merge', '--match-head-commit', head, '--body', 'text')\n")
        reviewed = {("p.py", "train"): ("fixed", "merge by PR number; carries no text", ["gh write merge --match-head-commit --merge"])}
        self.assertEqual(_audit({"ci.py": ast.parse(wrap), "p.py": ast.parse(src)}, reviewed)[0], [("p.py", "train")])

    def test_a_write_flag_glued_to_its_value_fails(self):
        src = {"s.py": ast.parse("import subprocess\n\ndef file(p, t):\n    subprocess.run(['gh', 'api', '-fbody=' + t, 'x'])\n"
                                 "\ndef tag(p, t):\n    subprocess.run(['gh', 'api', '--method=POST', 'x'])\n")}
        self.assertEqual(sorted(_audit(src, {})[0]), [("s.py", "file"), ("s.py", "tag")])

    def test_a_gh_write_with_a_global_flag_fails(self):
        src = {"s.py": ast.parse("import subprocess\n\ndef file(p, t):\n    subprocess.run(['gh', '-R', 'o/r', 'issue', 'create'])\n")}
        self.assertEqual(_audit(src, {})[0], [("s.py", "file")])

    def test_a_gh_argv_as_a_tuple_or_a_path_fails(self):
        src = {"s.py": ast.parse("import subprocess\n\ndef file(p, t):\n    subprocess.run(('/usr/bin/gh', 'issue', 'create'))\n")}
        self.assertEqual(_audit(src, {})[0], [("s.py", "file")])

    def test_a_write_through_a_module_attribute_wrapper_fails(self):
        wrapper = ast.parse("import subprocess\n\ndef _gh(p, *argv):\n    return subprocess.run(['gh', *argv])\n")
        caller = ast.parse("from sampo import ci\n\ndef report(p, title):\n    ci._gh(p, 'issue', 'create', '--title', title)\n")
        self.assertEqual(_audit({"ci.py": wrapper, "r.py": caller}, {})[0], [("r.py", "report")])

    def test_a_redacted_body_beside_a_project_handle_is_routed(self):
        wrapper = ast.parse("import subprocess\n\ndef _gh(p, *argv):\n    return subprocess.run(['gh', *argv])\n")
        caller = ast.parse("from sampo import core as c\nfrom sampo.ci import _gh\n\ndef post(p, body):\n"
                           "    body = c.redact_local(c.redact(body))\n"
                           "    _gh(p, 'pr', 'comment', '--body', body)\n")
        self.assertEqual(_audit({"ci.py": wrapper, "s.py": caller}, {})[0], [], "p is the project handle, not text: "
                         "the redacted body routes the post")

    def test_a_raw_body_beside_a_project_handle_fails(self):
        wrapper = ast.parse("import subprocess\n\ndef _gh(p, *argv):\n    return subprocess.run(['gh', *argv])\n")
        caller = ast.parse("from sampo.ci import _gh\n\ndef post(p, body):\n"
                           "    _gh(p, 'pr', 'comment', '--body', body)\n")
        self.assertEqual(_audit({"ci.py": wrapper, "s.py": caller}, {})[0], [("s.py", "post")])

    def test_a_redaction_under_a_branch_does_not_route_the_send(self):
        src = {"s.py": ast.parse("import urllib.request\nfrom sampo import core as c\n\ndef send(body, strict):\n"
                                 "    if strict:\n        body = c.redact_local(c.redact(body))\n"
                                 "    urllib.request.urlopen(urllib.request.Request('https://x.example', data=body.encode()))\n")}
        self.assertEqual(_audit(src, {})[0], [("s.py", "send")], "strict=False sends the raw text")

    def test_a_parameter_sent_without_redaction_fails_even_beside_a_redacted_name(self):
        src = {"s.py": ast.parse("import urllib.request\nfrom sampo import core as c\n\ndef send(raw):\n"
                                 "    body = c.redact_local(c.redact(raw))\n"
                                 "    urllib.request.urlopen(urllib.request.Request('https://x.example', "
                                 "data=(body + raw).encode()))\n")}
        self.assertEqual(_audit(src, {})[0], [("s.py", "send")])


class PrPostRedactsTheBody(ForemanTestCase):
    """T-5288: pr.post sends its body through redact_local strictly, before the publish grant is spent."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.fm("task", "new", "x", "--type", "FIX", "--tier", "S", "--step", "s", "--ac", "x :: true", "--focus")
        self.tid = c.active_brief(c.load_briefs(self.p)).id
        self.list = os.path.join(c.state_dir(), c.PRIVATE_TERMS)
        with open(self.list, "w") as f:
            f.write('["wombatcorp"]')
        self.posted = []

    def grant(self):
        b = c.find_brief(self.p, self.tid)
        b.meta["allow"] = ["publish"]
        c.save_brief(self.p, b)

    def post(self, body):
        def gh(p, *args):
            self.posted.append(args)
            return ""
        with mock.patch.object(pr, "_gh", side_effect=gh):
            return pr.post(self.p, c.find_brief(self.p, self.tid), body, 12)

    def test_the_body_goes_out_redacted_and_the_grant_is_spent(self):
        self.grant()
        self.assertEqual(self.post("wombatcorp at 10.0.0.1"), 12)
        self.assertEqual(self.posted, [("pr", "comment", "12", "--body", "<private> at <host>")])
        self.assertNotIn("publish", c.find_brief(self.p, self.tid).meta.get("allow") or [])

    def test_an_unreadable_list_refuses_the_post_before_the_grant_is_spent(self):
        self.grant()
        with open(self.list, "w") as f:
            f.write("{not json")
        with self.assertRaises(c.PrivateTermsUnreadable):
            self.post("wombatcorp")
        self.assertEqual(self.posted, [], "nothing posted")
        self.assertIn("publish", c.find_brief(self.p, self.tid).meta.get("allow") or [], "the grant is not spent")


if __name__ == "__main__":
    unittest.main()
