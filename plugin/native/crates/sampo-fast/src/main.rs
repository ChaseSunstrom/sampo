//! sampo-fast (T-3571): Sampo's native PreToolUse fast path, run by plugin/hooks/fast as
//! `sampo-fast --hook <plugin>/hooks/hook PreToolUse`.
//!
//! It only ever says "allow, silently": for the calls it can prove the Python hook (hooks/hook → sampo.hooks) lets
//! through with no output and no state change but its timing line, it appends that same `hook_ms` line to
//! events.jsonl (plus `"fast": true`) and exits 0. Every other call — another event or tool, odd input, a test knob
//! in the environment, a Python source changed since the build, any branch that reads more state than it mirrors —
//! goes to hooks/hook by exec, with the same argv, env and stdin bytes. It never blocks, prints or writes anything
//! else. Each mirrored Python function is named in MIRRORS; test_fast_equiv fails when one changes.
use serde_json::{Map, Value};
use std::ffi::{CString, OsString};
use std::fs::{self, File, OpenOptions};
use std::io::{self, ErrorKind, Read, Seek, SeekFrom, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileExt, MetadataExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{exit, Command, Stdio};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

include!(concat!(env!("OUT_DIR"), "/stamps.rs")); // WATCHED: (path under plugin/, size, mtime s, mtime ns)

// hooks.py and budget.py constants (compared with Python's through --consts)
const CRED_DIRS: [&str; 7] = [".ssh", ".aws", ".config/gcloud", ".kube", ".gnupg", ".config/gh", ".config/sampo"];
const CRED_FILES: [&str; 6] =
    [".netrc", ".npmrc", ".pypirc", ".git-credentials", ".claude/.credentials.json", ".docker/config.json"];
const OUTWARD: [&str; 6] =
    ["Artifact", "ArtifactComments", "ArtifactData", "PushNotification", "RemoteTrigger", "SendMessage"];
const SCHEDULERS: [&str; 3] = ["CronCreate", "RemoteTrigger", "ScheduleWakeup"];
const OUTWARD_MCP: &str = "mcp__claude_ai_Claude_Docs__";
const READS: [&str; 9] = ["export", "get", "guide", "list", "open", "profiles", "query", "quickstart", "read"];
const DOCS_TOOLS: [&str; 8] = ["batch", "create", "delete", "export", "guide", "query", "read", "update"];
const OUTLINE_LINES: u64 = 600;
const SNAP_SPAN: f64 = 21600.0;
const PACE_FLOOR: f64 = 75.0;
// sha1[:12] of each mirrored Python function's source (_run: less the other events' else branch; _pre_tool_use: up to
// its GUARDED check; hooks/hook: its event parse and dispatch)
const MIRRORS: [(&str, &str); 31] = [
    ("hooks.run", "e9a5910273f5"), ("hooks._run", "76bd3cd91b92"), ("hooks._pre_tool_use", "1b0be872783c"),
    ("hooks._credential_like", "a60da1d880d6"),
    ("hooks._credential_read", "6ed174ed2226"), ("hooks._outline_first", "08b8f3abe964"),
    ("hooks._outward_denial", "1e6d4418ddd5"), ("hooks._event", "debcc448ac4d"), ("hooks._rev", "7ee872410ee6"),
    ("hooks._cwd", "5dfb7c01da10"), ("budget.check_subagent", "4575be89ca9a"),
    ("budget.subagent_pause", "ac9deb5a8204"), ("budget.rate_limits", "295d742b9f04"),
    ("budget._recent_snaps", "fa6247e38a3c"), ("core.mirror_env", "e3ea72852a4e"), ("core.state_dir", "097be350d697"),
    ("core.foreman_home", "c516b6005ee9"), ("core.state_fallbacks", "5ad5949d011c"), ("core.gettempdir", "c30d095e6f0f"),
    ("core._marker_default", "cb40c01d2be1"), ("core._private", "7cfbfb951235"), ("core._writable", "3b299266b366"),
    ("core.now", "bde6d34a80ab"), ("core.clock", "6c9ac7951406"), ("core.host_load", "b2109cf9e763"),
    ("core.append_line", "f4d2c835d3b8"), ("core.find_project", "10122dfacd1b"), ("core._project", "5f97218e3254"),
    ("core.projects_dir", "576df9fb905e"), ("core.read_meta", "484af8e8f5ba"), ("hooks/hook", "66e8f167df00"),
];

type Obj = Map<String, Value>;

fn main() {
    let t0 = Instant::now();
    std::panic::set_hook(Box::new(|_| {})); // a caught panic defers quietly: stderr stays Python's
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    if args.first().is_some_and(|a| a == "--consts") {
        return consts();
    }
    if args.len() < 2 || args[0] != "--hook" {
        eprintln!("sampo-fast: usage: sampo-fast --hook <plugin>/hooks/hook <Event> (plugin/hooks/fast runs it)");
        exit(2); // never an allow
    }
    let (hook, rest) = (PathBuf::from(&args[1]), &args[2..]);
    let mut raw = Vec::new();
    let read = io::stdin().lock().read_to_end(&mut raw).is_ok();
    if read && std::panic::catch_unwind(|| allow(&hook, rest, &raw, t0)).unwrap_or(false) {
        exit(0);
    }
    defer(&hook, rest, &raw)
}

/// Run hooks/hook in this process: same argv and env, the same stdin bytes on fd 0.
fn defer(hook: &Path, rest: &[OsString], raw: &[u8]) -> ! {
    let err = match feed(raw) {
        Ok(stdin) => Command::new(hook).args(rest).stdin(stdin).exec(),
        Err(e) => e,
    };
    eprintln!("sampo-fast: couldn't hand this call to {} ({err}); it was blocked", hook.display());
    exit(2)
}

/// The payload as a readable fd: a pipe when it fits in one page (a pipe never holds less), else an unlinked file.
fn feed(raw: &[u8]) -> io::Result<Stdio> {
    if raw.len() <= 4096 {
        let (r, mut w) = io::pipe()?;
        w.write_all(raw)?;
        return Ok(r.into());
    }
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.subsec_nanos());
    let mut last = io::Error::from(ErrorKind::NotFound);
    for dir in [std::env::temp_dir(), "/dev/shm".into(), "/tmp".into()] {
        let path = dir.join(format!("sampo-fast-{}-{nanos}", std::process::id()));
        match OpenOptions::new().read(true).write(true).create_new(true).mode(0o600).open(&path) {
            Ok(mut f) => {
                let _ = fs::remove_file(&path);
                f.write_all(raw)?;
                f.seek(SeekFrom::Start(0))?;
                return Ok(f.into());
            }
            Err(e) => last = e,
        }
    }
    Err(last)
}

/// True once the call is decided natively and its line written; false hands it to Python.
fn allow(hook: &Path, rest: &[OsString], raw: &[u8], t0: Instant) -> bool {
    if rest.len() != 1 || rest[0] != "PreToolUse" || knobs() || !fresh(hook) {
        return false;
    }
    let Some(pl) = std::str::from_utf8(raw).ok().and_then(|s| serde_json::from_str::<Value>(s).ok()) else {
        return false;
    };
    let Value::Object(pl) = pl else { return false };
    let Some(Value::String(tool)) = pl.get("tool_name") else { return false };
    let Some(state) = state_dir() else { return false };
    let ok = match tool.as_str() {
        "Read" => read_ok(&pl),
        "Agent" | "Task" => agent_ok(&state),
        t if SCHEDULERS.contains(&t) => None,
        t if OUTWARD.contains(&t) => outward_ok(&pl, t, &state),
        t if t.strip_prefix(OUTWARD_MCP).is_some_and(|s| DOCS_TOOLS.contains(&s)) => outward_ok(&pl, t, &state),
        _ => None, // the guarded tools, and any tool this file doesn't know: Python's
    };
    let Some(rev) = ok.and_then(|_| rev(hook)) else { return false };
    log(&state, tool, &rev, t0);
    true
}

/// Environment that changes what Python does in ways this file doesn't mirror: test knobs, the opt-out, and a
/// variable this file reads that isn't UTF-8 (Python would still use it).
fn knobs() -> bool {
    let set = |k: &str| fenv(k).is_some_and(|v| !v.is_empty());
    let raw = |k: &str| std::env::var_os(k).is_some_and(|v| v.to_str().is_none());
    let names = ["FAST", "NOW", "HOST", "FAULT_LANE", "STATE", "HOME", "STATE_FALLBACK"];
    fenv("FAST").as_deref() == Some("0")
        || ["NOW", "HOST", "FAULT_LANE"].iter().any(|k| set(k))
        || std::env::var_os("SAMPO_HOOK_MODULES").is_some_and(|v| !v.is_empty())
        || std::env::var_os("PYTHON_CPU_COUNT").is_some()
        || names.iter().any(|k| raw(&format!("SAMPO_{k}")) || raw(&format!("FOREMAN_{k}")))
        || ["HOME", "XDG_STATE_HOME", "TMPDIR", "TEMP", "TMP"].iter().any(|k| raw(k))
}

/// FOREMAN_<k>, with SAMPO_<k> winning when set at all (core.mirror_env); None when unset or not UTF-8.
fn fenv(k: &str) -> Option<String> {
    match std::env::var_os(format!("SAMPO_{k}")) {
        Some(v) => v.into_string().ok(),
        None => std::env::var(format!("FOREMAN_{k}")).ok(),
    }
}

/// The mirrored sources are the ones this binary was built from (size and mtime, as the plugin resolves them).
fn fresh(hook: &Path) -> bool {
    let Some(plugin) = fs::canonicalize(hook).ok().and_then(|h| Some(h.parent()?.parent()?.to_path_buf())) else {
        return false;
    };
    WATCHED.iter().all(|(rel, size, s, ns)| {
        fs::metadata(plugin.join(rel)).is_ok_and(|m| m.size() == *size && m.mtime() == *s && m.mtime_nsec() == *ns)
    })
}

// ---------------------------------------------------------------- the tools

/// hooks._pre_tool_use's Read branch: _credential_read finds nothing and _outline_first returns None without state.
fn read_ok(pl: &Obj) -> Option<()> {
    let empty = Obj::new();
    let ti = match pl.get("tool_input") {
        None | Some(Value::Null) => &empty,
        Some(Value::Object(m)) => m,
        _ => return None,
    };
    let path = match ti.get("file_path") {
        None | Some(Value::Null) => return Some(()), // nothing to check, nothing to outline
        Some(Value::String(s)) => s.as_str(),
        _ => return None,
    };
    if !path.is_empty() {
        if path.contains('\0') || path.starts_with('~') {
            return None;
        }
        let joined = py_join(&cwd(pl)?, path);
        let real = fs::canonicalize(&joined).ok()?;
        if cred_like(&joined) || cred_like(real.to_str()?) {
            return None;
        }
    }
    if ["offset", "limit", "pages"].iter().any(|k| !matches!(ti.get(*k), None | Some(Value::Null))) {
        return Some(());
    }
    let sid = text_or(pl.get("session_id"), "")?;
    let who = text_or(pl.get("agent_id"), "main")?;
    match (word(sid), word(who)) {
        (Some(false), _) | (_, Some(false)) => return Some(()),
        (Some(true), Some(true)) => {}
        _ => return None,
    }
    let Ok(m) = fs::metadata(path) else { return Some(()) };
    if m.len() < OUTLINE_LINES * 4 {
        return Some(());
    }
    let mut buf = Vec::new();
    match File::open(path).and_then(|f| f.take(5_000_000).read_to_end(&mut buf)) {
        Ok(_) if (newlines(&buf) as u64) < OUTLINE_LINES => Some(()),
        Ok(_) => None, // its outline is due: Python's (state, the outline, a deny)
        Err(_) => Some(()),
    }
}

fn newlines(buf: &[u8]) -> usize {
    buf.iter().filter(|&&b| b == b'\n').count()
}

/// hooks._credential_like on one path string.
fn cred_like(p: &str) -> bool {
    let slashed = format!("{p}/");
    CRED_DIRS.iter().any(|d| slashed.contains(&format!("/{d}/"))) || CRED_FILES.iter().any(|f| p.ends_with(&format!("/{f}")))
}

/// str(v or default) for a string, null or absent v; None (defer) for any other type.
fn text_or<'a>(v: Option<&'a Value>, default: &'a str) -> Option<&'a str> {
    match v {
        None | Some(Value::Null) => Some(default),
        Some(Value::String(s)) => Some(if s.is_empty() { default } else { s }),
        _ => None,
    }
}

/// re.fullmatch(r"[\w-]{1,100}", s): Some(answer), or None when a non-ASCII letter leaves it to Python's \w.
fn word(s: &str) -> Option<bool> {
    let ascii_word = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';
    if s.is_empty() || s.chars().any(|c| c.is_ascii() && !ascii_word(c)) {
        return Some(false);
    }
    if s.is_ascii() { Some(s.len() <= 100) } else { None }
}

/// budget.check_subagent: no pause; a pace near the bar is Python's (weekly_lean needs the project).
fn agent_ok(state: &Path) -> Option<()> {
    // T-3902: a horizon in budget.json (budget set --until) makes the weekly pace Python's: this file mirrors only the floor
    if fs::read(state.join("budget.json")).is_ok_and(|b| b.windows(7).any(|w| w == b"\"until\"")) {
        return None;
    }
    let (five, week, gone) = rate_limits(state)?;
    if five.unwrap_or(0.0) >= 90.0 {
        return None;
    }
    if let Some(week) = week {
        let bar = PACE_FLOOR.max(90f64.min(gone.map_or(80.0, |g| 100.0 * g + 10.0)));
        if week >= bar {
            return None;
        }
    }
    Some(())
}

/// budget.rate_limits: (five_hour, seven_day, week_gone) of the newest snapshot that has any; None defers.
fn rate_limits(state: &Path) -> Option<(Option<f64>, Option<f64>, Option<f64>)> {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs_f64();
    'snaps: for path in recent_snaps(state)? {
        let Ok(data) = fs::read(&path) else { continue };
        let Value::Object(top) = serde_json::from_slice::<Value>(&data).ok()? else { continue }; // NaN: Python's
        let rl = match top.get("rate_limits") {
            v if !truthy(v) => None,
            Some(Value::Object(m)) => Some(m),
            _ => continue, // .get on a list: AttributeError, the next snapshot
        };
        let mut out = [None, None, None];
        for (i, k) in ["five_hour", "seven_day"].iter().enumerate() {
            let w = match rl.and_then(|m| m.get(*k)) {
                v if !truthy(v) => None,
                Some(Value::Object(m)) => Some(m),
                _ => continue 'snaps,
            };
            let Some(Value::Number(used)) = w.and_then(|m| m.get("used_percentage")) else { continue };
            let resets = match w.and_then(|m| m.get("resets_at")) {
                Some(Value::Number(n)) => Some(n.as_f64()?),
                Some(Value::Bool(b)) => Some(f64::from(u8::from(*b))), // a bool is an int to Python
                _ => None,
            };
            out[i] = Some(if resets.is_some_and(|r| r <= now) { 0.0 } else { used.as_f64()? });
            if let (1, Some(r)) = (i, resets) {
                if 0.0 < r - now && r - now <= 7.0 * 86400.0 {
                    out[2] = Some(1.0 - (r - now) / (7.0 * 86400.0));
                }
            }
        }
        if out[0].is_some() || out[1].is_some() {
            return Some((out[0], out[1], out[2]));
        }
    }
    Some((None, None, None))
}

/// budget._recent_snaps: sessions/*.json by mtime, newest first, the last three within SNAP_SPAN of the newest.
fn recent_snaps(state: &Path) -> Option<Vec<PathBuf>> {
    let Ok(dir) = fs::read_dir(state.join("sessions")) else { return Some(vec![]) }; // glob: [] on any error
    let mut snaps = vec![];
    for e in dir {
        let e = e.ok()?;
        let name = e.file_name();
        let name = name.to_str()?;
        if name.starts_with('.') || !name.ends_with(".json") {
            continue;
        }
        let m = fs::metadata(e.path()).ok()?; // Python's getmtime would raise
        snaps.push((m.mtime() as f64 + m.mtime_nsec() as f64 * 1e-9, e.path()));
    }
    snaps.sort_by(|a, b| a.0.total_cmp(&b.0)); // stable, as sorted(): ties keep the listing's order
    let Some(&(newest, _)) = snaps.last() else { return Some(vec![]) };
    let tail = &snaps[snaps.len().saturating_sub(3)..];
    Some(tail.iter().rev().filter(|s| newest - s.0 <= SNAP_SPAN).map(|s| s.1.clone()).collect())
}

/// hooks._outward_denial is None: a read action, or no project Python could find is sensitive (or unreadable).
fn outward_ok(pl: &Obj, tool: &str, state: &Path) -> Option<()> {
    let action = match pl.get("tool_input") {
        Some(Value::Object(m)) => match m.get("action") {
            Some(Value::String(s)) => s.as_str(),
            _ => "",
        },
        _ => "",
    };
    if READS.contains(&action) || tool.strip_prefix(OUTWARD_MCP).is_some_and(|t| READS.contains(&t)) {
        return Some(());
    }
    cwd(pl)?; // find_project(_cwd(pl)) raises on a cwd that isn't a string
    let dir = match fs::read_dir(state.join("projects")) {
        Ok(d) => d,
        Err(e) if e.kind() == ErrorKind::NotFound => return Some(()),
        Err(_) => return None,
    };
    for e in dir {
        match fs::read(e.ok()?.path().join("meta.json")) {
            Ok(data) => {
                let Value::Object(meta) = serde_json::from_slice::<Value>(&data).ok()? else { return None };
                if truthy(meta.get("sensitive")) {
                    return None;
                }
            }
            Err(e) if matches!(e.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {}
            Err(_) => return None,
        }
    }
    Some(())
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|x| x != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(m)) => !m.is_empty(),
    }
}

/// hooks._cwd: the payload's cwd, else this process's; None for a type Python would trip on.
fn cwd(pl: &Obj) -> Option<String> {
    match pl.get("cwd") {
        Some(Value::String(s)) if !s.is_empty() => (!s.contains('\0')).then(|| s.clone()),
        None | Some(Value::Null) | Some(Value::String(_)) => std::env::current_dir().ok()?.into_os_string().into_string().ok(),
        _ => None,
    }
}

/// os.path.join(a, b) for two strings.
fn py_join(a: &str, b: &str) -> String {
    if b.starts_with('/') || a.is_empty() {
        b.to_string()
    } else if a.ends_with('/') {
        format!("{a}{b}")
    } else {
        format!("{a}/{b}")
    }
}

// ---------------------------------------------------------------- state and the line

/// core.state_dir, for the cases with no fallback in play; None when a fallback marker or an unwritable home
/// would make Python look further.
fn state_dir() -> Option<PathBuf> {
    if let Some(v) = fenv("STATE").filter(|v| !v.is_empty()) {
        return abspath(&v);
    }
    let home = home()?;
    let fhome = match fenv("HOME").filter(|v| !v.is_empty()) {
        Some(h) => abspath(&h)?,
        None => Path::new(&home).join(".claude/foreman"),
    };
    let default = fhome.join("state");
    let xdg = std::env::var("XDG_STATE_HOME").ok().filter(|v| !v.is_empty());
    let mut alts = vec![Path::new(&xdg.unwrap_or_else(|| format!("{home}/.local/state"))).join("foreman")];
    let uid = unsafe { getuid() };
    for t in ["TMPDIR", "TEMP", "TMP"].iter().filter_map(|k| std::env::var(k).ok().filter(|v| !v.is_empty())) {
        if !t.starts_with('/') {
            return None; // core.gettempdir() (tempfile's candidates and test) would resolve it against the cwd
        }
        alts.push(Path::new(&t).join(format!("foreman-state-{uid}")));
    }
    for t in ["/tmp", "/var/tmp", "/usr/tmp"] {
        alts.push(Path::new(t).join(format!("foreman-state-{uid}")));
    }
    for alt in alts {
        match fs::symlink_metadata(alt.join(".foreman-state.json")) {
            Err(e) if matches!(e.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {}
            _ => return None, // a marker may move the state: Python's to follow
        }
    }
    (writable(&default) || fenv("STATE_FALLBACK").as_deref() == Some("0")).then_some(default)
}

/// os.path.expanduser("~"): HOME without its trailing slashes.
fn home() -> Option<String> {
    let h = std::env::var("HOME").ok().filter(|h| h.starts_with('/'))?;
    let h = h.trim_end_matches('/');
    Some(if h.is_empty() { "/".into() } else { h.into() })
}

/// os.path.abspath for a path with no ~ (and not the POSIX // root).
fn abspath(p: &str) -> Option<PathBuf> {
    if p.starts_with('~') {
        return None;
    }
    let full = if p.starts_with('/') { p.to_string() } else { py_join(std::env::current_dir().ok()?.to_str()?, p) };
    if full.starts_with("//") && !full.starts_with("///") {
        return None;
    }
    let mut parts: Vec<&str> = vec![];
    for c in full.split('/') {
        match c {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            c => parts.push(c),
        }
    }
    Some(PathBuf::from(format!("/{}", parts.join("/"))))
}

/// core._writable: the path, or its nearest existing ancestor, passes access(W_OK).
fn writable(p: &Path) -> bool {
    let mut p = p;
    loop {
        if p.exists() {
            let Ok(c) = CString::new(p.as_os_str().as_bytes()) else { return false };
            return unsafe { access(c.as_ptr(), 2) } == 0;
        }
        match p.parent() {
            Some(q) => p = q,
            None => return false,
        }
    }
}

extern "C" {
    fn getuid() -> u32;
    fn access(path: *const std::ffi::c_char, mode: std::ffi::c_int) -> std::ffi::c_int;
    fn getloadavg(loads: *mut f64, n: std::ffi::c_int) -> std::ffi::c_int;
    fn sysconf(name: std::ffi::c_int) -> std::ffi::c_long;
}

#[cfg(target_os = "linux")]
const SC_NPROCESSORS_ONLN: std::ffi::c_int = 84;
#[cfg(target_os = "macos")]
const SC_NPROCESSORS_ONLN: std::ffi::c_int = 58;

/// hooks._rev: plugin.json's version where hooks/hook resolves; None (defer) for a value redact might touch.
fn rev(hook: &Path) -> Option<String> {
    let plugin = fs::canonicalize(hook).ok()?.parent()?.parent()?.to_path_buf();
    let Ok(data) = fs::read(plugin.join(".claude-plugin/plugin.json")) else { return Some("?".into()) };
    let v = match serde_json::from_slice::<Value>(&data).ok()? {
        Value::Object(m) => m.get("version").cloned(),
        _ => return Some("?".into()),
    };
    match v {
        v if !truthy(v.as_ref()) => Some("?".into()),
        Some(Value::String(s)) if s.split('.').count() <= 3 && s.split('.').all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit())) => Some(s),
        _ => None,
    }
}

/// hooks.run's hook_ms line, appended as core.append_line does; a failure is ignored, as hooks._event does.
fn log(state: &Path, tool: &str, rev: &str, t0: Instant) {
    let ms = pyround(t0.elapsed().as_secs_f64() * 1000.0, 1);
    let mut line = format!(
        "{{\"ts\": \"{}\", \"kind\": \"hook_ms\", \"event\": \"PreToolUse\", \"ms\": {ms}, \"rev\": \"{rev}\", \"tool\": \"{tool}\"",
        now()
    );
    let mut la = [0f64; 3];
    let cpus = unsafe { sysconf(SC_NPROCESSORS_ONLN) }.max(1) as f64; // os.cpu_count() or 1
    if unsafe { getloadavg(la.as_mut_ptr(), 3) } >= 1 {
        line += &format!(", \"load\": {}", pyround(la[0] / cpus, 2));
    }
    line += ", \"fast\": true}\n";
    let _ = fs::create_dir_all(state).and_then(|_| append(&state.join("events.jsonl"), line.as_bytes()));
}

/// core.append_line: one write; a torn last line gets its newline first.
fn append(path: &Path, data: &[u8]) -> io::Result<()> {
    let mut f = OpenOptions::new().read(true).append(true).create(true).open(path)?;
    let size = f.metadata()?.len();
    let mut last = *b"\n";
    if size > 0 {
        f.read_exact_at(&mut last, size - 1)?;
    }
    let lead: &[u8] = if last[0] == b'\n' { b"" } else { b"\n" };
    f.write_all(&[lead, data].concat())
}

/// round(x, n) printed as Python's repr prints the result (never an exponent at these sizes).
fn pyround(x: f64, n: usize) -> String {
    let r: f64 = format!("{x:.n$}").parse().unwrap_or(0.0);
    let s = format!("{r}");
    if s.contains('.') { s } else { format!("{s}.0") }
}

/// core.now(): UTC, to the second.
fn now() -> String {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs()) as i64;
    let (days, rem) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    let z = days + 719468; // civil_from_days (H. Hinnant)
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

fn consts() {
    let list = |xs: &[&str]| Value::from(xs.iter().map(|s| Value::from(*s)).collect::<Vec<_>>());
    let mirrors: Obj = MIRRORS.iter().map(|(k, v)| (k.to_string(), Value::from(*v))).collect();
    let out = serde_json::json!({
        "cred_dirs": list(&CRED_DIRS), "cred_files": list(&CRED_FILES), "outward": list(&OUTWARD),
        "schedulers": list(&SCHEDULERS), "outward_mcp": OUTWARD_MCP, "reads": list(&READS),
        "outline_lines": OUTLINE_LINES, "snap_span": SNAP_SPAN as u64, "pace_floor": PACE_FLOOR as u64,
        "untimed": false, "watched": list(&WATCHED.map(|w| w.0)), "mirrors": mirrors,
    });
    println!("{out}");
}
