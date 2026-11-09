"""fm research ask (T-0206): web research as an engine, not one agent's word. What the project already knows comes
first (fm recall); the question is split into sub-questions (by a tool-less planner child, or --sub); each is researched
by its own `claude -p` child that can only search and fetch the web; every claim must cite a page and quote it; then
each cited page is fetched here and the quoted words are looked for, so a claim is marked verified, not found or
unchecked before anyone relies on it. One note goes to the project's research/ (recall finds it next time).

Only public https pages are fetched (every redirect re-checked), at most 3 MB each."""
import hashlib
import html
import http.client
import ipaddress
import itertools
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from sampo import budget
from sampo import core as c

NO_MCP = ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}})]
WEB_TOOLS = "WebSearch,WebFetch"
PLANNER = ("Split the question below into 2-{n} sub-questions that together answer it and can each be researched on "
           "the web on their own. Reply with only the sub-questions, one per line, each starting with '- '.")
RESEARCHER = """You research one sub-question on the web for a software engineer. Use WebSearch, then WebFetch the most
authoritative pages (official docs, source repositories, specs, papers, maintainers' posts before blogs and forums).
Reply with claim lines only, in exactly this format, one per line:
- CLAIM: <one specific, checkable statement> | SOURCE: <the https URL you fetched> | QUOTE: "<5-25 words copied exactly from that page>" | TIER: primary|secondary|community | DATE: <the page's publication or update date, or unknown>
Then the strongest evidence against your own answer, same format but starting "- AGAINST:" (search for it on purpose).
Then what you could not find out: "- OPEN: <question>".
Rules: every CLAIM and AGAINST quotes words that really are on the cited page (they are checked by fetching it);
prefer recent sources and say when a source is older than a year; no claim without a source; 3-10 claims."""
_FIELD = re.compile(r"\|\s*(SOURCE|QUOTE|TIER|DATE):\s*", re.I)
MAX_PAGE = 3_000_000
FETCH_S = 30  # a whole page, not each read: a server dripping bytes can't hold a worker
MIN_QUOTE = 4  # words: "the" is on every page


class Claim:
    def __init__(self, text, url, quote, tier, date, kind="claim"):
        self.text, self.url, self.quote, self.tier, self.date, self.kind = text, url, quote, tier, date, kind
        self.status, self.why = "unchecked", "not checked"
        self.sub, self.model, self.support = 0, "", ""  # T-0229: which sub-question and model, and who else says it


def parse_claims(text):
    """{"claims": [Claim], "open": [str]} from a researcher's claim lines; other lines are ignored."""
    claims, open_ = [], []
    for line in text.splitlines():
        m = re.match(r"\s*[-*]\s*(CLAIM|AGAINST|OPEN):\s*(.+)$", line, re.I)
        if not m:
            continue
        kind, rest = m.group(1).lower(), m.group(2)
        if kind == "open":
            open_.append(rest.strip())
            continue
        parts = _FIELD.split(rest)
        fields = {k.upper(): v.strip() for k, v in zip(parts[1::2], parts[2::2])}
        claims.append(Claim(parts[0].strip(), fields.get("SOURCE", ""), fields.get("QUOTE", "").strip('"“” '),
                            fields.get("TIER", "").lower() or "unknown", fields.get("DATE", "unknown"), kind))
    return {"claims": claims, "open": open_}


def public_https(url):
    """True for an https URL whose host resolves only to public addresses (no localhost, LAN or cloud metadata)."""
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or not u.hostname:
            return False
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except (ValueError, OSError):
        return False
    # ponytail: resolved here and again by urllib (DNS rebinding could differ); pin the address if that ever matters
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_https(newurl):
            raise urllib.error.URLError(f"redirect to a non-public or non-https URL ({c.fit(newurl, 80)})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    """(page text, None) or (None, why)."""
    if not public_https(url):
        return None, "not a public https URL"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Sampo research check)",
                                               "Accept": "text/html,text/plain,*/*"})
    deadline, raw = time.monotonic() + FETCH_S, b""
    try:
        with urllib.request.build_opener(_Redirect).open(req, timeout=15) as resp:
            while len(raw) < MAX_PAGE and time.monotonic() < deadline:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                raw += chunk
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        return None, c.fit(str(getattr(e, "reason", e)) or type(e).__name__, 80)
    return _decode(raw, charset), None


def _decode(raw, charset):
    try:
        return raw.decode(charset, "replace")
    except LookupError:  # a charset the server made up
        return raw.decode("utf-8", "replace")


def _norm(text):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = text.translate(str.maketrans("‘’“”–— ", "''\"\"-- "))
    return re.sub(r"\s+", " ", text).strip().lower()


def _words(text):
    """T-0313: the text's words alone, padded, for an in-order match that markup can't break (markdown escapes,
    highlighted code, \\" in a quote were 6 of 16 real quotes marked not found)."""
    return " " + " ".join(re.findall(r"[^\W_]+", _norm(text))) + " "


def cached_fetch(url, fetch=fetch):
    """T-0239: a page fetched at most once a day — verification and repeat asks read the cached text (successes only;
    earlier days are pruned when a page is written)."""
    root, day = os.path.join(c.state_dir(), "research-cache"), time.strftime("%Y-%m-%d")
    path = os.path.join(root, day, hashlib.sha256(url.encode()).hexdigest()[:40] + ".txt")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), None
    except OSError:
        pass
    page, why = fetch(url)
    if page is not None and len(page) >= 500 and not _SOFT_ERROR.search(page[:4000]):  # review: a challenge or an
        try:                                                                              # error page isn't cached
            for old in os.listdir(root) if os.path.isdir(root) else []:
                if old != day and re.fullmatch(r"\d{4}-\d\d-\d\d", old):
                    shutil.rmtree(os.path.join(root, old), ignore_errors=True)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            c.write_atomic(path, page)
        except OSError:
            pass  # a cache that can't be written costs a refetch, never the check
    return page, why


_SOFT_ERROR = re.compile(r"(?i)just a moment\.\.\.|cf-chl|attention required|access denied|enable javascript and "
                         r"cookies|are you a robot|captcha")
_NEG = re.compile(r"\b(not|never|cannot|can't|doesn't|don't|isn't|aren't|won't|deprecated|unsupported)\b", re.I)


def _domain(url):
    """The registrable part of a URL's host — docs.alpha.dev and blog.alpha.dev are one source, not two."""
    # ponytail: last two labels (three under a 2-letter country code's com/co/org…); the public suffix list if needed
    labels = (urllib.parse.urlsplit(url or "").hostname or "").lower().split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "net", "ac", "gov",
                                                                             "edu") else 2
    return ".".join(labels[-keep:]) if labels != [""] else ""


def cross_check(claims, models=()):
    """T-0229: each claim's support — another site saying the same ('also: …', '(unchecked)' when that quote couldn't be
    checked; a quote not on its page doesn't count) or none ('single source'); with two models whether both said it —
    and possible contradictions: across sub-questions or models, a similar AGAINST line, or a similar claim differing
    in negation. Word overlap (≥ 3 stems and half the union), so it flags, it doesn't judge. [(claim, other)]"""
    from sampo import recall
    words = {id(x): set(recall._tokens(x.text)) for x in claims}

    def similar(x, y):
        a, b = words[id(x)], words[id(y)]
        return len(a & b) >= 3 and len(a & b) >= 0.5 * len(a | b)
    conflicts, seen = [], set()
    answered = {(x.sub, x.model) for x in claims}  # a model that said nothing about a sub-question can't disagree
    for x in claims:
        if x.kind != "claim":
            continue
        like = [y for y in claims if y is not x and similar(x, y)]
        agree = [y for y in like if y.kind == "claim" and y.status != "not found"
                 and bool(_NEG.search(y.text)) == bool(_NEG.search(x.text))]
        others = {}
        for y in agree:
            d = _domain(y.url)
            if d and d != _domain(x.url):
                others[d] = others.get(d) or y.status == "verified"
        x.support = ("also: " + ", ".join(d + ("" if ok else " (unchecked)") for d, ok in sorted(others.items())[:2])
                     if others else "single source")
        if len(models) > 1:
            other = next(m for m in models if m != x.model) if x.model in models else None
            x.support += (" · both models" if any(y.model != x.model for y in agree) else
                          f" · only {x.model}" if (x.sub, other) in answered else f" · {other} gave no answer")
        for y in like:
            key = frozenset((x.text, y.text))
            if (y.sub != x.sub or y.model != x.model) and key not in seen and (
                    y.kind == "against" or bool(_NEG.search(y.text)) != bool(_NEG.search(x.text))):
                seen.add(key)  # one line per pair of statements, however many models repeated them
                conflicts.append((x, y))
    return conflicts


def verify(claims, fetch=None):
    """Mark each claim verified (its quote is on its page), not found, or unchecked (no source, or the page can't be
    read: a 403, a JavaScript-only page). Pages come through the day's cache unless a fetch is given."""
    fetch = fetch or cached_fetch
    def safe(url):  # one bad page costs its claims, never the run (and the paid research with it)
        try:
            return fetch(url)
        except Exception as e:
            return None, f"{type(e).__name__}: {c.fit(str(e), 60)}"
    urls = sorted({x.url for x in claims if x.url and x.quote})
    with ThreadPoolExecutor(8) as pool:
        pages = dict(zip(urls, pool.map(safe, urls)))
    for x in claims:
        if not x.url or not x.quote:
            x.status, x.why = "unchecked", "no source or quote"
            continue
        page, why = pages[x.url]
        if page is None:
            x.status, x.why = "unchecked", why
        elif len(_words(x.quote).split()) < MIN_QUOTE:
            x.status, x.why = "unchecked", f"quote too short to check (under {MIN_QUOTE} words)"
        elif _norm(x.quote) in _norm(page) or _words(x.quote) in _words(page):  # CJK has no word gaps to pad
            x.status, x.why = "verified", ""
        else:
            x.status, x.why = "not found", "the quoted words aren't on the page"


def _child(model, tools, system):
    c.refuse_if_paused()  # T-0591
    return ["claude", "-p", "--model", model, "--no-session-persistence", "--output-format", "json",
            "--max-budget-usd", f"{budget.effective()[0]['run']:g}",  # T-0776
            "--setting-sources", "project,local",
            "--tools", tools, *(["--allowed-tools", tools] if tools else []), *NO_MCP, "--append-system-prompt", system]


def _run(jobs, timeout):
    """[(text or None, error)] for [(argv, stdin)], run in parallel in a scratch folder (T-0295: fmideas' runner)."""
    from sampo import ideas
    return ideas.run_children([(argv, stdin, "") for argv, stdin in jobs], timeout, "research")


MARK = {"verified": "✓", "not found": "✗", "unchecked": "?"}


def _how(x):
    """T-2397: how a claim was learned: read (a cited page), ran (its source is a command) or recalled (no source)."""
    u = (x.url or "").strip()
    return "ran" if u.startswith(("run:", "`")) else "read" if u.startswith(("http://", "https://")) else "recalled"


def _line(x):
    return (f"- {MARK[x.status]} [{_how(x)}] {x.text} — {x.url or 'no source'}" + (f' "{x.quote}"' if x.quote else "")
            + f" ({x.tier}, {x.date}" + (f"; {x.status}: {x.why}" if x.status != "verified" else "; quote found") + ")"
            + (f" · {x.support}" if x.support else ""))


def cmd_ask(args):
    from sampo import cli
    c.refuse_if_paused()  # T-0591
    p = cli.resolve(args)
    if args.file:  # T-0239: a queue of questions, one note each (each budget-checked on its own)
        if args.name or args.sub or args.question:
            raise cli.UsageError("--file takes no question, --name or --sub: each line is planned and named on its own")
        try:
            with open(args.file, encoding="utf-8", errors="replace") as f:
                lines = [ln.strip()[:500] for ln in itertools.islice(f, 1000)]  # bounded however big the file is
        except OSError as e:
            raise cli.UsageError(f"--file: {e.strerror or e}")
        qs = [ln for ln in lines if ln and not ln.startswith("#")]
        if not qs or len(qs) > 20:
            raise cli.UsageError(f"{args.file}: {len(qs) or 'no'} questions — give 1 to 20, one per line")
        notes, failed = [], []
        for q in qs:  # review: one failing question doesn't hide the notes already written, or stop the rest
            try:
                notes.append(_ask(p, args, q)[0])
            except cli.UsageError as e:
                failed.append({"question": q, "error": str(e)})
        cli.out(args, {"notes": notes, "failed": failed}, "\n".join(
            [f"Research note: {n['path']} ({n['claims']} claims, {n['conflicts']} possible conflict(s))" for n in notes]
            + [f"failed: {x['question']} — {x['error']}" for x in failed]))
        unverified = sum(n.get("not_found") or 0 for n in notes)
        if unverified:
            print(f"sampo: {unverified} claim(s) quote words that aren't on the cited page: treat them as unverified",
                  file=sys.stderr)
        return 1 if failed else 0
    q = (args.question or "").strip()
    if not q:
        raise cli.UsageError("ask what? (a question, or --file FILE with one per line)")
    res, text, count = _ask(p, args, q)
    cli.out(args, res, text)
    if count["not found"]:
        print(f"sampo: {count['not found']} claim(s) quote words that aren't on the cited page: treat them as unverified",
              file=sys.stderr)


def _ask(p, args, q):
    """(result, summary text, counts) for one question: recall, plan, research (on two models with --quorum), verify,
    cross-check, one note."""
    from sampo import cli
    from sampo import recall
    # most of the question's words, or it isn't known yet
    known = [h for h in recall.recall(p, q, n=5, cover=0.67) if h[1] in ("research", "brief", "decision")]
    subs = [s.strip() for s in args.sub or [] if s.strip()]
    try:
        budget.check("research", budget.estimate("research", (0 if subs else 1) + min(len(subs) or args.fanout,
                                                                                          args.fanout)
                                                     * (2 if args.quorum else 1), 0.2),
                       "a smaller --fanout" + (", or no --quorum" if args.quorum else ""))
    except budget.BudgetError as e:
        raise cli.UsageError(str(e))
    try:
        if not subs:
            [(plan, err)] = _run([(_child(args.model, "", PLANNER.format(n=args.fanout)), f"Question: {q}\n")],
                                 args.timeout)
            subs = [ln.strip()[2:].strip() for ln in (plan or "").splitlines() if ln.strip().startswith("- ")]
            if not subs:
                raise cli.UsageError(f"the planner gave no sub-questions ({err or c.fit(plan or '', 120)}); "
                                       f"pass them with --sub")
        subs = subs[:args.fanout]
        models = [args.model] + ([args.quorum] if args.quorum and args.quorum != args.model else [])
        runs = {m: _run([(_child(m, WEB_TOOLS, RESEARCHER), f"Question: {q}\nSub-question: {s}\n") for s in subs],
                        args.timeout) for m in models}  # T-0229 --quorum: the same researchers on a second model
    except OSError as e:
        raise cli.UsageError(str(e))
    answers = runs[args.model]
    parsed = {m: [parse_claims(a or "") for a, _ in got] for m, got in runs.items()}
    for m, per_sub in parsed.items():
        for i, got in enumerate(per_sub):
            for x in got["claims"]:
                x.sub, x.model = i, m
    claims = [x for per_sub in parsed.values() for got in per_sub for x in got["claims"]]
    if not claims:
        why = "; ".join(err or c.fit(c.plain(a or ""), 100) for a, err in answers)
        raise cli.UsageError(f"no claims came back ({why})")
    if not args.no_verify:
        verify(claims)
    conflicts = cross_check(claims, models)
    unique = list({(x.sub, x.kind, x.text, x.url): x for x in claims}.values())  # both models' copies count once
    count = {k: sum(x.status == k for x in unique) for k in MARK}
    single = sum(x.kind == "claim" and x.support.startswith("single source") for x in unique)
    body = [f"# Research: {q}", "",
            f"Asked {c.now()[:10]} · {len(subs)} sub-question(s) · {len(unique)} claims: {count['verified']} quote "
            f"found, {count['not found']} quote not found, {count['unchecked']} unchecked. ✓ means the quoted words "
            f"are on the cited page, not that the claim is true; 'also:', 'single source' and possible conflicts come "
            f"from word overlap between claims, not from reading them.", ""]
    if known:
        body += ["## Already known (fm recall)"] + [f"- [recalled] {h[2]}" for h in known] + [""]
    for i, (s, (_, err)) in enumerate(zip(subs, answers)):
        mine, listed = [x for x in claims if x.sub == i], set()
        lines = []
        for x in mine:  # the same claim from both models is listed once (its support says both)
            if (x.kind, x.text, x.url) not in listed:
                listed.add((x.kind, x.text, x.url))
                lines.append((x.kind, _line(x)))
        opens = [o for got in (per_sub[i] for per_sub in parsed.values()) for o in got["open"]]
        body += [f"## {s}"] + [ln for k, ln in lines if k == "claim"]
        against = [ln for k, ln in lines if k == "against"]
        body += (["", "Against:"] + against if against else []) + (["", "Open:"] + [f"- {o}" for o in dict.fromkeys(opens)]
                                                                     if opens else [])
        body += [f"- ({m} failed: {got[i][1]})" if len(models) > 1 else f"- (failed: {got[i][1]})"
                 for m, got in runs.items() if got[i][1]]  # review: a second model that failed is named, not silent
        body.append("")
    if conflicts:  # T-0229: answers that disagree — settle these before a decision rests on either
        body += ["## Possible conflicts (similar claims that disagree, across sub-questions or models)"] + [
            f"- {x.text} ({_domain(x.url) or 'no source'}) ⟷ {y.text} ({_domain(y.url) or 'no source'})"
            for x, y in conflicts] + [""]
    body += ["## Decision", "- Recommendation: (what to do, decided from the claims above)",  # T-0599
             "- Would change if: (the fact that would flip it)",
             f"- Record it: fm task set {args.task or 'ID'} --section Decision --text \"Recommendation: …; would change "
             f"if: …\" (a RESEARCH task closing without one warns)", ""]
    name = args.name or "ask-" + (re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")[:40] or "q") + "-" \
        + time.strftime("%Y%m%d-%H%M")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", name):
        raise cli.UsageError(f"research name must be a plain file name, got {name!r}")
    path = os.path.join(p.dir, "research", name + ".md")
    with c.lock(p.dir):
        n = 2
        while not args.name and os.path.exists(path):  # review: two asks in one minute keep both notes
            path = os.path.join(p.dir, "research", f"{name}-{n}.md")
            n += 1
        name = os.path.basename(path)[:-3]
        c.write_atomic(path, c.defang(c.redact("\n".join(body))))
        c.log_event(p, "research", task=args.task, data={"name": name, "claims": len(unique), **count,
                                                          "conflicts": len(conflicts), "single": single},
                    session=cli.session())
        c.regen_views(p)
    res = {"path": path, "subs": subs, "claims": len(unique), "known": [h[2] for h in known], **count,
           "conflicts": len(conflicts), "single_source": single, "models": models,
           "failed": [err for got in runs.values() for _, err in got if err]}
    text = (f"Research note: {path}\n  {len(unique)} claims over {len(subs)} sub-question(s): "
            f"{count['verified']} ✓ quote found, {count['not found']} ✗ not on the page, {count['unchecked']} ? "
            f"unchecked · {single} single-source · {len(conflicts)} possible conflict(s)"
            + (f"\n  already known: {'; '.join(c.fit(h[2], 80) for h in known[:3])}" if known else "")
            + "".join(f"\n  failed: {e}" for e in res["failed"]))
    return {k.replace(" ", "_"): v for k, v in res.items()}, text, count
