"""T-3742: models stay newest and best, by default (models.newest, on).

Settings side: ~/.claude/settings.json's `model` stays an alias ('best' unless `sampo models pin` names another), which
Claude Code re-resolves each session from its own served catalogue, so a new release is adopted with no Sampo release;
`switchModelsOnFlag` is true so Claude Code's one-time refusal-fallback question can't stall an unattended run. Codex
gets the same rule from its own model cache.

Session side: Claude Code's refusal fallback moves a session to a lower model for the rest of the session. The
PostModelSwitch hook (source auto) or the transcript's model_refusal_fallback entry records it; the band and pane show
it with the way back; and after the refusal's own turn the session is sent `/model best` (tmux, when its prompt is
empty) or an SDK set_model request (headless). One repeat refusal ends the attempts. The user's choices live in
models.json (`newest`, `pin`, `pin_codex`); per-session records in state/models/<session>.json."""
import json
import os
import re
import subprocess
import time

from sampo import core as c

WAY_BACK = "/model best"
FAMILY = {"haiku": 1, "sonnet": 2, "opus": 3, "fable": 4}
PENDING_S = 120  # our own /model best is allowed past Claude Code's cache-miss confirm for this long
RECENT_S = 12 * 3600  # a downgrade older than this is no longer shown
KEEP_S = 30 * 86400
SCAN_BYTES = 256 * 1024  # a long turn's transcript growth is read from its end, never whole


# ---------------------------------------------------------------- the user's setting

def _cfg_path():
    return os.path.join(c.foreman_home(), "models.json")


def _cfg():
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _put(key, value):
    d = _cfg()
    if value is None:
        d.pop(key, None)
    else:
        d[key] = value
    c.write_atomic(_cfg_path(), json.dumps(d, indent=2) + "\n")


def newest_on():
    return _cfg().get("newest") is not False


def pinned(codex=False):
    v = _cfg().get("pin_codex" if codex else "pin")
    return v if isinstance(v, str) and v.strip() else None


def set_newest(on):
    _put("newest", None if on else False)


def set_pin(name, codex=False):
    _put("pin_codex" if codex else "pin", name or None)


# ---------------------------------------------------------------- the alias

_FULL = re.compile(r"claude-(fable|opus|sonnet|haiku)-\d[\w.-]*?(\[1m\])?")


def target_model(cur, pin=None):
    """The `model` setting newest-and-best wants: a pin wins; none or a stale alias is 'best'; a full id is its alias;
    anything else (sonnet, haiku, opus[1m], opusplan, a cloud ARN) is a deliberate choice and stays."""
    if pin:
        return pin
    if not cur:
        return "best"
    if not isinstance(cur, str):
        return cur
    low = cur.strip().lower()
    if low in ("best", "opus", "default", "inherit"):
        return "best"
    m = _FULL.fullmatch(low)
    if m:
        fam, one = m.groups()
        return f"{fam}[1m]" if one else "best" if fam in ("fable", "opus") else fam
    return cur


def apply_newest(settings):
    """A copy of settings with the alias and the flag set; unchanged (a copy) when models.newest is off."""
    out = dict(settings)
    if not newest_on():
        return out
    out["model"] = target_model(out.get("model"), pinned())
    out["switchModelsOnFlag"] = True
    return out


def model_rank(model):
    """(family, version) for ordering Claude models; None for anything else."""
    low = str(model or "").lower()
    m = re.search(r"(fable|opus|sonnet|haiku)(.*)", low)
    if not m:
        return None
    nums = [int(n) for n in re.findall(r"\d+", m.group(2)) if len(n) <= 2][:2]  # a date suffix is no version
    return FAMILY[m.group(1)], tuple(nums)


def codex_newest(codex_dir=None):
    """Codex when present: its config.toml's top-level model moves to the top listed model of its own cache unless
    pinned or off. 'old -> new' when changed, else None. The cache format is as Codex 0.x writes it; anything odd
    changes nothing."""
    d = codex_dir or os.path.join(os.path.expanduser("~"), ".codex")
    cfg = os.path.join(d, "config.toml")
    if not newest_on() or pinned(codex=True) or not os.path.isfile(cfg):
        return None
    try:
        with open(os.path.join(d, "models_cache.json"), encoding="utf-8") as f:
            rows = [r for r in json.load(f)["models"] if isinstance(r, dict) and r.get("slug")
                    and r.get("visibility", "list") == "list" and isinstance(r.get("priority"), (int, float))]
        best = min(rows, key=lambda r: r["priority"])["slug"]
        with open(cfg, encoding="utf-8") as f:
            text = f.read()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    head = re.search(r"(?m)^\[", text)
    top = text[:head.start()] if head else text  # a `model =` inside a [table] is not the top-level one
    m = re.search(r'(?m)^model\s*=\s*"([^"]*)"', top)
    if not m or m.group(1) == best or not isinstance(best, str) or not re.fullmatch(r"[\w.:/-]+", best):
        return None
    c.write_atomic(cfg, text[:m.start(1)] + best + text[m.end(1):])
    return f"{m.group(1)} -> {best}"


# ---------------------------------------------------------------- the verb

def cmd_models(args):
    """sampo models [status | newest on|off | pin NAME|off [--codex]]: the setting, and the settings.json it keeps."""
    from sampo import setup
    w = list(args.words or [])
    if w[:1] == ["newest"] and w[1:2] in (["on"], ["off"]):
        set_newest(w[1] == "on")
    elif w[:1] == ["pin"] and len(w) == 2:
        set_pin(None if w[1] == "off" else w[1], codex=args.codex)
    elif w and w != ["status"]:  # T-3612's typo list: 'models list' meant the local profiles; exit 2 as argparse does
        print(f"sampo models: no {w[0]!r} here (status | newest on|off | pin NAME|off [--codex]); did you mean "
              "code models (the local model profiles)?", file=__import__("sys").stderr)
        raise SystemExit(2)
    if w and w != ["status"]:
        changed = setup.fix_model()
        if changed:
            print(changed)
    s = setup._load(setup.paths()["settings"], None) or {}
    print(f"models.newest: {'on' if newest_on() else 'off'}; pin: {pinned() or 'none'}"
          f"; codex pin: {pinned(codex=True) or 'none'}; settings.json model: {s.get('model', 'unset')}, "
          f"switchModelsOnFlag: {s.get('switchModelsOnFlag', 'unset')}")
    for r in downgraded():
        print(f"  session {r['sid']} dropped {r['from']} -> {r['to']}: {r['way_back']}")


# ---------------------------------------------------------------- a session's downgrade

def _dir():
    return os.path.join(c.state_dir(), "models")


def _file(sid):
    return os.path.join(_dir(), re.sub(r"[^\w.-]", "_", str(sid))[:80] + ".json")


def _load(sid):
    try:
        with open(_file(sid), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(sid, st):
    c.write_atomic(_file(sid), json.dumps(st))


def _prune():
    try:
        for n in os.listdir(_dir()):
            p = os.path.join(_dir(), n)
            if n.endswith(".json") and os.path.isfile(p) and time.time() - os.path.getmtime(p) > KEEP_S:
                os.remove(p)
    except OSError:
        pass


def _down(st, frm, to, source):
    if st.get("restores"):  # it was switched back once and refused again: no ping-pong
        st["gave_up"] = True
    st["down"] = {"from": frm, "to": to, "source": source, "ts": time.time(), "stops": 0, "compacted": False,
                  "sent": None}


def on_switch(pl):
    """PostModelSwitch: a refusal fallback (source auto, to a lower model) is recorded; a switch up, or one the user
    made, clears the record."""
    sid = pl.get("session_id")
    if not sid:
        return None
    to, frm, src = str(pl.get("to_model") or ""), str(pl.get("from_model") or ""), pl.get("source")
    rt, rf = model_rank(to), model_rank(frm)
    with c.lock(_dir(), timeout=5):
        st = _load(sid)
        if src == "auto" and rt and rf and rt < rf:
            _down(st, frm, to, "auto")
            _prune()
        elif st.get("down"):
            was = model_rank(st["down"].get("from"))
            if src in ("command", "picker", "sdk") or (rt and was and rt >= was):
                st["down"] = None
        _save(sid, st)
    return None


def on_compact(pl):
    sid = pl.get("session_id")
    if sid and os.path.exists(_file(sid)):
        with c.lock(_dir(), timeout=5):
            st = _load(sid)
            if st.get("down"):
                st["down"]["compacted"] = True
                _save(sid, st)
    return None


def pre_switch(pl):
    """PreModelSwitch: our own /model best skips Claude Code's prompt-cache-miss confirm."""
    st = _load(pl.get("session_id")) if pl.get("session_id") else {}
    sent = (st.get("down") or {}).get("sent") or 0
    if sent and time.time() - sent < PENDING_S:
        return {"hookSpecificOutput": {"hookEventName": "PreModelSwitch", "permissionDecision": "allow",
                                       "permissionDecisionReason": "Sampo is switching the session back up"}}
    return None


def _scan(sid, st, path):
    """The transcript's growth since the last Stop: a model_refusal_fallback entry the session still sits on."""
    try:
        size = os.path.getsize(path)
        off = st.get("tr_off", 0) if st.get("tr_off", 0) <= size else 0
        st["tr_off"] = size
        if size <= off:
            return
        with open(path, "rb") as f:
            f.seek(max(off, size - SCAN_BYTES))
            chunk = f.read()
    except OSError:
        return
    if b"model_refusal_fallback" not in chunk:
        return
    fb, after = None, None
    for ln in chunk.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("subtype") == "model_refusal_fallback":
            fb, after = e, None
        elif fb and e.get("type") == "assistant" and isinstance(e.get("message"), dict):
            after = e["message"].get("model") or after
    if not fb or (after and after != fb.get("fallbackModel")):
        return
    to, frm = str(fb.get("fallbackModel") or ""), str(fb.get("originalModel") or "")
    if (st.get("down") or {}).get("to") == to or not (model_rank(to) and model_rank(frm)):
        return
    _down(st, frm, to, "transcript")


def _headless():
    return bool(os.environ.get("FOREMAN_DRIVE_TASK")) or os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-cli"


def _prompt_empty(pane):
    try:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    for ln in reversed(out.stdout.splitlines()[-12:]):
        m = re.match(r"^\s*[│|]?\s*[>❯]\s*(.*?)\s*[│|]?\s*$", ln)
        if m:
            return not m.group(1)
    return False  # no prompt in sight: busy, or not a Claude Code pane


def _send(sid):
    """Switch the session up through the channel it has; the channel's name, or None when it has none right now."""
    if _headless():  # a host that runs the SDK registers an inbox here and forwards each line to Claude Code's stdin
        inbox = os.path.join(_dir(), "sdk", re.sub(r"[^\w.-]", "_", str(sid))[:80] + ".jsonl")
        if not os.path.isfile(inbox):
            return None
        req = {"type": "control_request", "request_id": f"sampo-{int(time.time())}",
               "request": {"subtype": "set_model", "model": "best"}}
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(json.dumps(req) + "\n")
        return "sdk"
    try:
        with open(os.path.join(c.state_dir(), "sessions", re.sub(r"[^\w.-]", "_", str(sid)) + ".json"), encoding="utf-8") as f:
            pane = (json.load(f) or {}).get("tmux_pane")
    except (OSError, ValueError, AttributeError):
        return None
    if not pane or not re.fullmatch(r"%\d+", str(pane)) or not _prompt_empty(pane):
        return None
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", WAY_BACK], capture_output=True, timeout=10, check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return "tmux"


def after_stop(pl):
    """Stop: look for a fallback the hook missed; once the refusal's own turn is over (a second stop, or a compaction)
    switch the session back, once, unless it was refused again after an earlier switch back."""
    sid = pl.get("session_id")
    if not sid or not newest_on():
        return None
    path = pl.get("transcript_path")
    if not os.path.exists(_file(sid)) and not (path and os.path.isfile(path)):
        return None
    with c.lock(_dir(), timeout=5):
        st = _load(sid)
        if path:
            _scan(sid, st, path)
        d = st.get("down")
        if d:
            d["stops"] = d.get("stops", 0) + 1
            if not st.get("gave_up") and not d.get("sent") and (d["stops"] >= 2 or d.get("compacted")) and _send(sid):
                d["sent"] = time.time()
                st["restores"] = st.get("restores", 0) + 1
        _save(sid, st)
    return None


def downgraded():
    """Sessions sitting on a lower model after a fallback: what the band and the pane show."""
    out = []
    try:
        names = sorted(os.listdir(_dir()))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(_dir(), n), encoding="utf-8") as f:
                st = json.load(f)
            d = st.get("down")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(d, dict) and time.time() - d.get("ts", 0) < RECENT_S:
            way = (WAY_BACK + " or /clear switches back; refused again, so Sampo stopped trying") if st.get("gave_up") \
                else (WAY_BACK + " sent; /model best if it didn't land") if d.get("sent") else WAY_BACK
            out.append({"sid": n[:-5], "from": d.get("from"), "to": d.get("to"), "ts": d.get("ts"),
                        "sent": bool(d.get("sent")), "gave_up": bool(st.get("gave_up")), "way_back": way})
    return out
