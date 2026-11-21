"""T-2389 (Mission 7, #10): fm prove --release. The artifact the manifest declares is built in a throwaway copy with no
network (unshare -n where allowed, offline flags everywhere), installed into a throwaway prefix with no package index,
and each declared entry point runs from that prefix (never from the source tree, where a file the artifact left out
still exists) with --version, else --help. The installed version must match the manifest, and a version an entry
prints must too. A build or install that needs a download is unproven, naming what it needed; nothing is published.
Python: the project's own PEP 517 backend builds the wheel (what pip wheel --no-build-isolation does), the wheel is
unpacked into the prefix; npm: npm pack, the tarball unpacked as node_modules/NAME; gem: gem build, gem install --local
--ignore-dependencies; Rust: cargo install --offline --root PREFIX (a crate builds only with its dependencies cached)."""
import glob
import json
import os
import re
import shutil
import tarfile
import time
import zipfile

from sampo import core as c

RUN_T = 240
MISSING = re.compile(r"No module named '?([\w.]+)'?")
DOWNLOAD = re.compile(r"(?i)offline|failed to download|could not resolve|no matching package|ENOTCACHED|"
                      r"network|getaddrinfo|Could not find a version|unable to get (?:local )?issuer")
VERSION = re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:[-.+]\w+)*")


class Outcome(Exception):
    def __init__(self, verdict, why):
        super().__init__(why)
        self.verdict, self.why = verdict, why


def _run(argv, cwd, env, end, sandbox=()):
    from sampo import migrate
    return migrate._exec(list(sandbox) + argv, cwd, env, end)


def _said(out, err, why, tmp):
    from sampo import migrate
    return migrate._say(why or err or out, tmp)


def _entry(name, argv, cwd, env, end, want, tmp):
    """One entry point: --version, else --help; (row, failure or '')."""
    for flag in ("--version", "--help"):
        out, err, rc, why = _run(argv + [flag], cwd, env, end)
        if rc == 0 and not why:
            got = VERSION.findall(out) if flag == "--version" else []
            if got and want and want not in got:
                return {"name": name, "flag": flag, "exit": 0}, f"{name} --version prints {got[0]}, the manifest says {want}"
            return {"name": name, "flag": flag, "exit": 0}, ""
    return {"name": name, "flag": "--help", "exit": rc}, f"{name} doesn't run from the installed artifact: " \
                                                        f"{_said(out, err, why, tmp)}"


def _python(work, prefix, env, end, tmp, sandbox):
    import tomllib
    with open(os.path.join(work, "pyproject.toml"), "rb") as f:
        meta = tomllib.load(f)
    proj, bs = meta.get("project") or {}, meta.get("build-system") or {}
    want = proj.get("version") if isinstance(proj.get("version"), str) else None
    backend = str(bs.get("build-backend") or "setuptools.build_meta:__legacy__")
    out_dir = os.path.join(tmp, "dist")
    os.makedirs(out_dir)
    code = ("import importlib,sys;m,_,o=sys.argv[1].partition(':');b=importlib.import_module(m)\n"
            "for a in filter(None,o.split('.')):b=getattr(b,a)\nprint(b.build_wheel(sys.argv[2]))")
    out, err, rc, why = _run(["python3", "-c", code, backend, out_dir], work, env, end, sandbox)
    if rc or why:
        miss = MISSING.search(err or "")
        if miss or DOWNLOAD.search(err or out or ""):
            raise Outcome("unproven", f"the build needs {miss.group(1) if miss else 'a download'}, which isn't installed "
                                      f"here (fm prove --release builds offline)")
        raise Outcome("fail", f"the wheel didn't build: {_said(out, err, why, tmp)}")
    wheels = glob.glob(os.path.join(out_dir, "*.whl"))
    if not wheels:
        raise Outcome("fail", "the build said it worked but left no wheel")
    site = os.path.join(prefix, "site")
    with zipfile.ZipFile(wheels[0]) as z:
        for n in z.namelist():  # a wheel's names stay inside the prefix, or it isn't installed
            if n.startswith("/") or ".." in n.split("/"):
                raise Outcome("fail", f"the wheel holds a path outside its install folder: {c.fit(c.plain(n), 80)}")
        z.extractall(site)
    info = next(iter(glob.glob(os.path.join(site, "*.dist-info"))), "")
    got = re.search(r"(?m)^Version:\s*(\S+)", _read(os.path.join(info, "METADATA")))
    version = got.group(1) if got else None
    if want and version != want:
        raise Outcome("fail", f"the wheel says version {version}, the manifest says {want}")
    entries, in_toml = [], proj.get("scripts") or {}
    text = _read(os.path.join(info, "entry_points.txt"))
    body = text.split("[console_scripts]", 1)[1].split("\n[", 1)[0] if "[console_scripts]" in text else ""
    scripts = dict(re.findall(r"(?m)^\s*([\w.-]+)\s*=\s*([\w.]+:[\w.]+)", body))
    for name in sorted(set(in_toml) - set(scripts)):  # declared, but the artifact doesn't install it
        raise Outcome("fail", f"{name} is declared in pyproject.toml but the wheel installs no such command")
    deps = {re.split(r"[\s<>=!~;\[]", d, 1)[0].lower().replace("-", "_") for d in proj.get("dependencies") or []}
    for name, target in sorted(scripts.items()):
        mod, _, attr = target.partition(":")
        launch = (f"import sys;sys.path.insert(0,{site!r});sys.argv=[{name!r}]+sys.argv[1:]\n"
                  f"from {mod} import {attr.split('.')[0]} as f\n" + "".join(f"f=f.{a}\n" for a in attr.split(".")[1:])
                  + "sys.exit(f())")
        row, bad = _entry(name, ["python3", "-I", "-c", launch], prefix, env, end, want, tmp)
        if bad:
            miss = MISSING.search(bad)
            if miss and miss.group(1).split(".")[0].lower() in deps:
                raise Outcome("unproven", f"{name} needs its dependency {miss.group(1)}, and installing it needs a download")
            raise Outcome("fail", f"{bad}" + (f" (the wheel lacks {miss.group(1)})" if miss else ""))
        entries.append(row)
    return {"kind": "python", "artifact": os.path.basename(wheels[0]), "version": version, "entries": entries}


def _npm(work, prefix, env, end, tmp, sandbox):
    with open(os.path.join(work, "package.json"), encoding="utf-8") as f:
        meta = json.load(f)
    name, want = str(meta.get("name") or ""), meta.get("version")
    if meta.get("dependencies"):
        raise Outcome("unproven", f"installing {name} needs {c.joinmore(sorted(meta['dependencies']), 5)} from the registry")
    out_dir = os.path.join(tmp, "dist")
    os.makedirs(out_dir)
    out, err, rc, why = _run(["npm", "pack", "--offline", "--silent", "--pack-destination", out_dir], work, env, end, sandbox)
    tars = glob.glob(os.path.join(out_dir, "*.tgz"))
    if rc or why or not tars:
        raise Outcome("unproven" if DOWNLOAD.search(err or out or "") else "fail",
                      f"npm pack didn't make the tarball: {_said(out, err, why, tmp)}")
    dest = os.path.join(prefix, "node_modules", *name.split("/"))
    with tarfile.open(tars[0]) as t:
        t.extractall(os.path.join(tmp, "unpacked"), filter="data")  # the data filter refuses links and paths outside
    shutil.copytree(os.path.join(tmp, "unpacked", "package"), dest)
    with open(os.path.join(dest, "package.json"), encoding="utf-8") as f:
        version = json.load(f).get("version")
    if want and version != want:
        raise Outcome("fail", f"the tarball says version {version}, the manifest says {want}")
    bins = meta.get("bin") or {}
    bins = {name.split("/")[-1]: bins} if isinstance(bins, str) else bins
    entries = []
    for exe, rel in sorted(bins.items()):
        path = os.path.join(dest, str(rel))
        if not os.path.isfile(path):
            raise Outcome("fail", f"{exe}: {rel} is declared as its bin but the tarball leaves it out")
        row, bad = _entry(exe, ["node", path], prefix, env, end, want, tmp)
        if bad:
            raise Outcome("fail", bad)
        entries.append(row)
    return {"kind": "npm", "artifact": os.path.basename(tars[0]), "version": version, "entries": entries}


def _gem(work, prefix, env, end, tmp, sandbox):
    spec = sorted(glob.glob(os.path.join(work, "*.gemspec")))[0]
    text = _read(spec)
    want = (re.search(r"""\.version\s*=\s*["']([^"']+)""", text) or [None, None])[1]
    out, err, rc, why = _run(["gem", "build", os.path.basename(spec)], work, env, end, sandbox)
    gems = glob.glob(os.path.join(work, "*.gem"))
    if rc or why or not gems:
        raise Outcome("unproven" if DOWNLOAD.search(err or out or "") else "fail",
                      f"gem build didn't make the gem: {_said(out, err, why, tmp)}")
    version = (re.search(r"-(\d[^/]*)\.gem$", gems[0]) or [None, None])[1]
    if want and version != want:
        raise Outcome("fail", f"the gem says version {version}, the gemspec says {want}")
    genv = dict(env, GEM_HOME=prefix, GEM_PATH=prefix)
    out, err, rc, why = _run(["gem", "install", "--local", "--ignore-dependencies", "--no-document", "--install-dir", prefix,
                              "--bindir", os.path.join(prefix, "bin"), gems[0]], tmp, genv, end, sandbox)
    if rc or why:
        raise Outcome("fail", f"gem install didn't install it: {_said(out, err, why, tmp)}")
    entries = []
    for exe in sorted(os.listdir(os.path.join(prefix, "bin"))) if os.path.isdir(os.path.join(prefix, "bin")) else []:
        row, bad = _entry(exe, [os.path.join(prefix, "bin", exe)], prefix, genv, end, want, tmp)
        if bad:
            raise Outcome("fail", bad)
        entries.append(row)
    return {"kind": "gem", "artifact": os.path.basename(gems[0]), "version": version, "entries": entries}


def _rust(work, prefix, env, end, tmp, sandbox):
    import tomllib
    with open(os.path.join(work, "Cargo.toml"), "rb") as f:
        meta = tomllib.load(f)
    want = (meta.get("package") or {}).get("version")
    home = os.path.expanduser("~")  # the toolchain and any cached crates are the user's; the build itself stays offline
    renv = dict(env, RUSTUP_HOME=os.environ.get("RUSTUP_HOME") or os.path.join(home, ".rustup"),
                CARGO_HOME=os.environ.get("CARGO_HOME") or os.path.join(home, ".cargo"), CARGO_NET_OFFLINE="true",
                CARGO_TARGET_DIR=os.path.join(tmp, "target"))
    out, err, rc, why = _run(["cargo", "install", "--offline", "--path", ".", "--root", prefix], work, renv, end, sandbox)
    if rc or why:
        raise Outcome("unproven" if DOWNLOAD.search(err or out or "") else "fail",
                      f"cargo install didn't build it offline: {_said(out, err, why, tmp)}")
    entries = []
    for exe in sorted(os.listdir(os.path.join(prefix, "bin"))):
        row, bad = _entry(exe, [os.path.join(prefix, "bin", exe)], prefix, renv, end, want, tmp)
        if bad:
            raise Outcome("fail", bad)
        entries.append(row)
    return {"kind": "rust", "artifact": "cargo install", "version": want, "entries": entries}


KINDS = (("pyproject.toml", _python), ("package.json", _npm), ("*.gemspec", _gem), ("Cargo.toml", _rust))


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(1 << 20)
    except OSError:
        return ""


def prove(root, allowed, limit=RUN_T):
    from sampo import migrate
    from sampo import prove as sprove
    r = {"root": root, "entries": []}
    if not allowed:
        return r | {"verdict": "refused", "why": sprove.no_run()}
    tmp, work = sprove._copy(root)
    end = time.time() + limit
    try:
        build = next((fn for pat, fn in KINDS if glob.glob(os.path.join(work, pat))), None)
        if not build:
            return r | {"verdict": "nothing", "why": "no manifest to build from (pyproject.toml, package.json, a .gemspec, "
                                                     "Cargo.toml)"}
        env = dict(migrate._env(root, tmp), PIP_NO_INDEX="1", npm_config_offline="true")
        prefix = os.path.join(tmp, "prefix")
        os.makedirs(prefix)
        got = build(work, prefix, env, end, tmp, migrate._sandbox(env, root, work, end))
        return r | got | {"verdict": "pass", "why": f"{got['artifact']} {got['version'] or ''} installed into a throwaway "
                                                   f"prefix; {len(got['entries'])} entry point(s) ran from it".replace("  ", " ")}
    except Outcome as e:
        return r | {"verdict": e.verdict, "why": e.why}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, tarfile.TarError) as e:
        return r | {"verdict": "fail", "why": f"the artifact couldn't be read: {c.fit(c.plain(str(e)), 200)}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_release(args, root, allowed):
    from sampo import cli
    r = prove(root, allowed)
    lines = [f"{r['verdict']}: {r['why']}"] + [f"  {e['name']} {e['flag']} → exit {e['exit']}" for e in r["entries"]]
    cli.out(args, r, c.plain_lines("\n".join(lines)))
    return {"pass": 0, "fail": 1}.get(r["verdict"], 2)
