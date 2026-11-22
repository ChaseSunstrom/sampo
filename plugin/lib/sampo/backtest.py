"""T-1058: fm backtest: where a changed rule, known fix or card would have fired on recorded sessions, and how those
calls went. Read-only; it reads the session logs fm replay reads and writes nothing."""
import re

from sampo import replay

HAND_CMD_MAX = 2000  # the characters of a by-hand command a hand pattern reads (the hook's cut, examgrade.CMD_MAX)


def _hand(name, card):
    """T-5612: a calling card's hand patterns (its "hand" list of regexes, the by-hand Bash runs it would replace),
    compiled, or UsageError naming what's missing. A stopgap: once T-5604's shared callcards.hand_patterns (with
    load-time validation) lands, it replaces this check and the loader's."""
    from sampo import cli
    pats = card.get("hand")
    if not isinstance(pats, list) or not pats:
        raise cli.UsageError(f'card {name} has no hand patterns: add "hand": [regex, ...] to its card.json')
    out = []
    for p in pats:
        if not isinstance(p, str):
            raise cli.UsageError(f"card {name}: hand patterns are strings (regexes), not {p!r}")
        try:
            out.append(re.compile(p))
        except re.error as e:
            raise cli.UsageError(f"card {name}: hand pattern {p!r} is not a regex: {e}")
    return out


def cmd_backtest(args):
    from sampo import callcards
    from sampo import cli
    from sampo import lib
    if sum(bool(x) for x in (args.rule, args.fix, args.card)) != 1:
        raise cli.UsageError("fm backtest takes one of --rule REGEX, --fix SIGNATURE, --card NAME")
    card = None  # T-5612: set when NAME is a calling card, whose result bytes are counted too
    if args.rule:
        try:
            rx = re.compile(args.rule)
        except re.error as e:
            raise cli.UsageError(f"--rule is not a regex: {e}")
        hit, what = (lambda ev: bool(rx.search(ev["text"]))), f"rule /{args.rule}/"
    elif args.fix:
        sig = args.fix.lower()  # a fix fires on the failure text, so only on calls that failed
        hit, what = (lambda ev: ev["errored"] and sig in ev["result"].lower()), f"fix `{args.fix}`"
    elif any(x["stem"] == args.card for x in lib.cards()):  # the stack library wins a name both shelves hold
        rx = re.compile(r"(?<![\w-])" + r"[-\s]".join(map(re.escape, args.card.split("-"))) + r"(?![\w-])", re.I)
        hit, what = (lambda ev: bool(rx.search(ev["text"]))), f"card {args.card}"
    else:
        # the cards `sampo card list` shows from this repo, so a name here is one the lead can call
        card = callcards.cards(callcards._root(args))[0].get(args.card)
        if card is None:
            raise cli.UsageError(f"no card named {args.card!r} (fm lib list, or sampo card list for calling cards)")
        pats = _hand(args.card, card)
        # the hook's gate: a Bash call's command line only (a description or other input never matches), cut at
        # HAND_CMD_MAX; the same words in a Read or an Edit are not a by-hand run
        hit = lambda ev: ev.get("tool") == "Bash" and any(  # noqa: E731
            p.search(ev["command"][:HAND_CMD_MAX]) for p in pats)
        what = f"card {args.card}"
    n = fires = errored = result_bytes = 0
    scanned, fired = set(), set()
    for ev in replay.events(args.days):  # one pass: a long log is never held whole
        n += 1
        scanned.add(ev["session"])
        if not hit(ev):
            continue
        fires += 1
        fired.add(ev["session"])
        errored += bool(ev["errored"])
        result_bytes += len(ev["result"].encode("utf-8", "replace"))
    d = {"fires": fires, "sessions": len(fired), "errored": errored, "calls": n, "scanned_sessions": len(scanned)}
    if card is not None:  # gross: every matching call's output, errored or not, no once-per-session limit
        d["result_bytes"] = result_bytes
        d["tokens_read_est"] = result_bytes // 4
    if not n:
        text = f"no recent session logs in the last {args.days} days: nothing to backtest {what} on"
    else:
        text = (f"{what} would have fired {d['fires']} times in {d['sessions']} session(s) "
                f"of {d['scanned_sessions']} scanned ({d['calls']} calls); {d['errored']} followed by an error")
        if card is not None:
            text += (f"; reading {result_bytes} bytes (about {d['tokens_read_est']} tokens, gross) across the matching "
                     "calls, errored or not, each call counted (no once-per-session limit)")
    return cli.out(args, d, text)
