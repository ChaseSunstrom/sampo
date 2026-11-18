"""T-2388 (Mission 7, #10): the incident ritual. `fm capture --incident '<impact>' --signal CMD` runs the harm signal and opens an
urgent FIX only when it is red (exit 1..125; a 126/127, a timeout or a run that never reached its check is a broken
signal, not harm). The red run is the first evidence of criterion 1 ("the signal is green again"), the Timeline section
starts at the UTC time, and `fm capture --incident log '<action>' --expect '<effect>'` adds a row. The close needs the same signal
green twice after the fix's last edit (both fm-run: the close's own run counts as one) and one captured follow-up
(`fm capture --incident follow-up '<text>'`). The signal stays behind as criterion 1's passing run, and fm sentinel's re-verify queue
reads the incident's touched files as its scope, so a later commit touching them reruns it. The signal is stored on the
brief and runs through the guard like any verify, with credentials stripped from its environment; criterion 1 must still be
that command at the close (re-open the incident to change it)."""
import argparse
import os
import re

from sampo import core as c

SECTION = "Timeline"
_TS = re.compile(r"\((\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\)$")


def _fmcli():
    from sampo import cli
    return cli


def _env():
    from sampo import bench
    # T-2388 review: every credential is stripped, the model's own (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN) included:
    # a signal is any command the session or the user wrote
    return {k: v for k, v in os.environ.items() if k != "FOREMAN_SKIP_TESTS" and not bench.CREDENTIALS.match(k)}


def _open_incident(p, tid=None):
    bs = [b for b in c.load_briefs(p) if b.meta.get("incident_signal") and b.status not in c.CLOSED
          and (tid is None or b.id == tid)]
    if not bs:
        raise _fmcli().UsageError(f"no open incident{f' {tid}' if tid else ''}: fm capture --incident '<impact>' --signal CMD opens one")
    return next((b for b in bs if b.status == "active"), bs[-1])


def cmd_incident(args):
    f = _fmcli()
    p = f.resolve(args)
    if args.signal is None:
        verb, rest = args.words[0], " ".join(args.words[1:]).strip()
        if verb == "log" and rest:
            return _log(p, args, rest)
        if verb == "follow-up" and rest:
            return _followup(p, args, rest)
        raise f.UsageError("fm capture --incident '<impact>' --signal CMD opens one; then fm capture --incident log '<action>' --expect "
                           "'<effect>', and fm capture --incident follow-up '<what stops this recurring>'")
    return _open(p, args, c.plain(c.redact(" ".join(args.words))).strip(), args.signal.strip())


def _open(p, args, impact, signal):
    f = _fmcli()
    if not impact or not signal or re.search(r"[`\n\r]", signal):
        raise f.UsageError("fm capture --incident '<impact>' --signal CMD: the impact in words, the signal one command on one line "
                           "(no backticks: the evidence line can't hold them)")
    f.guard_stored(p, None, signal)
    code, output = c.run_command(p.root, signal, 120, env=_env())
    if not code:
        raise f.UsageError(f"the signal passes now (`{c.fit(signal, 80)}` → exit 0): nothing is failing, so no incident "
                           f"is opened; a signal that is green during harm doesn't see it")
    if code == 124 or code > 125 or code < 0 or c.never_ran(signal, code, output):
        raise f.UsageError(f"signal broken, not harm: `{c.fit(signal, 80)}` → {c.run_result(code, output, signal)}; "
                           f"fix the signal so a healthy system reads exit 0 and a harmed one 1..125")
    tree, now = c.worktree_id(p.root), c.now()
    with c.lock(p.dir):
        b = f._create(p, f"Incident: {impact}"[:100], "FIX", "S", "planned", raw=impact, source="incident",
                      priority="urgent")
        b.meta["incident_signal"] = signal
        b.append_log("incident opened on a red signal")
        b.add_ac(f"the signal is green again: `{c.fit(signal, 80)}` exits 0 twice after the fix", signal)
        b.add_step("Fix it: record the signal green (fm task evidence %s --ac 1 --run '<signal>'), capture one follow-up "
                   "(fm capture --incident follow-up '<what stops this recurring>'), then fm task finish" % b.id)
        b.set_section(SECTION, f"- {now} opened: {impact}\n")
        b.add_evidence(signal, c.run_result(code, output, signal), ac=1, tree=tree, ran=True, ts=now)
        c.save_brief(p, b)
        c.log_event(p, "incident", task=b.id, data={"signal": signal[:200], "exit": code}, session=f.session())
        c.regen_views(p)
    f.out(args, {"id": b.id, "signal": signal, "exit": code},
          f"{b.id}: incident opened (urgent FIX, signal red: exit {code}). Timeline: fm capture --incident log '<action>' --expect "
          f"'<effect>'; close needs the signal green twice, then one follow-up.")
    f.cmd_focus(argparse.Namespace(id=b.id, project=getattr(args, "project", None), json=False))


def _log(p, args, action):
    f = _fmcli()
    if not args.expect or not args.expect.strip():
        raise f.UsageError("fm capture --incident log '<action>' --expect '<effect>': a row says what was done and what should change")
    row = f"- {c.now()} {c.plain(c.redact(action))} (expect: {c.plain(c.redact(args.expect)).strip()})"
    b = _open_incident(p, getattr(args, "id", None))
    f.mutate(p, b.id, lambda x: x._append_line(SECTION, row), "incident_log", {"action": action[:200]})
    f.out(args, {"id": b.id, "row": row}, f"{b.id}: timeline row added.")


def _followup(p, args, text):
    f = _fmcli()
    b = _open_incident(p, getattr(args, "id", None))
    with c.lock(p.dir):
        if followups(p, b):
            raise f.UsageError(f"{b.id} has its follow-up ({followups(p, b)[0]}): one is the close's rule; more go "
                               f"through fm capture --source followup")
        n = f._create(p, f._title(text), "FEATURE", c.guess_tier("FEATURE", text), "captured",
                      raw=f"{text}\n\nFollow-up of incident {b.id}", source="followup")
        n.meta["incident"] = b.id
        c.save_brief(p, n, touch=False)
        c.log_event(p, "capture", task=n.id, data={"source": "followup", "type": "FEATURE", "incident": b.id},
                    session=f.session())
        c.regen_views(p)
    f.out(args, {"id": n.id, "incident": b.id}, f"{n.id}: follow-up captured for {b.id}.")


def followups(p, b):
    return [x.id for x in c.load_briefs(p) if x.meta.get("incident") == b.id]


def _greens(b, since):
    """Passing fm runs of criterion 1's signal at or after `since` (whole-second clocks: the same second counts)."""
    sig = c.redact(b.meta["incident_signal"]).replace("`", "'")
    return sum(1 for l in b.evidence() if l.startswith("- (ac 1) ") and c._RAN_MARK in l and f"`{sig}` → exit 0" in l
               and (m := _TS.search(l)) and m.group(1) >= since)


def gaps(p, b, closing=False):
    """What stops an incident's close; [] for any other task. closing: fm task finish is about to run criterion 1 and
    records that run, so it counts as one of the two."""
    sig = b.meta.get("incident_signal")
    if not sig or b.status in c.CLOSED:
        return []
    out = []
    if dict(b.verify_cmds()).get(1) != sig:
        return [f"the incident's signal changed (criterion 1 is no longer `{c.fit(sig, 60)}`): re-open the incident "
                f"(fm capture --incident '<impact>' --signal CMD) rather than edit its check"]
    sig_e = c.redact(sig).replace("`", "'")
    if not any(l.startswith("- (ac 1) ") and c._RAN_MARK in l and f"`{sig_e}` → ✗ exit" in l for l in b.evidence()):
        out.append("the signal's red run is missing from the evidence: re-open the incident")
    ac1 = next((a for a in b.acceptance() if a.n == 1), None)
    n = _greens(b, c.last_change(p, b.id)) + (1 if closing and ac1 and not ac1.checked else 0)
    if n < 2:
        out.append(f"the signal must be green twice after the fix's last edit ({n} so far; both fm runs): "
                   f"fm task evidence {b.id} --ac 1 --run '{c.fit(sig, 80)}'")
    if not followups(p, b):
        out.append(f"one captured follow-up is the close's price: fm capture --incident follow-up '<what stops this recurring>' ({b.id})")
    return out


def safe_gaps(p, b, closing=False):
    """gaps for the close gates; a reader that breaks must not stop a close: it fails open and logs where hooks' errors go."""
    try:
        return gaps(p, b, closing)
    except Exception:
        import traceback
        try:
            from sampo import hooks  # T-3431: rotation and repeat collapse; lazy, the failure path only
            hooks.log_error("fmincident.gaps", traceback.format_exc())
        except Exception:
            pass
        return []


def scope(b):
    """The files fm sentinel watches for a closed incident: what its fix touched (recorded at the close)."""
    return [x.lstrip("- ").strip() for x in b.section("Files touched").splitlines() if x.strip()] \
        if b.meta.get("incident_signal") else []
