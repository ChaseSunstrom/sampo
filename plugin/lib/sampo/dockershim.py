"""T-3746 (T-3747: build, rmi, copy_in for Terminal-Bench): the docker-exec tool shim. A Container is one `sampo-bench-*` container; its read, search, edit, write and run
return the same result text Smith's own tools do, so a Smith run can work inside it (bench_swe, and Terminal-Bench's
adapter). Nothing here touches a container or image that isn't ours: every name starts with PREFIX, and prune() removes
only those.

The docker binary is `docker` or $SAMPO_DOCKER (tests point it at a fake). No library is needed."""
import os
import shlex
import shutil
import subprocess
import uuid

PREFIX = "sampo-bench-"
OUT_CAP = 12000      # characters of a command's output the model sees: the head and the tail of this
RUN_SECONDS = 600    # one run action; the container's `timeout` kills it, not the docker client
# the official SWE-bench images keep their Python in a conda env named testbed; other images have no such file
ACTIVATE = "[ -f /opt/miniconda3/bin/activate ] && source /opt/miniconda3/bin/activate && conda activate testbed; "
LINE_MAX, READ_LINES = 500, 200


class DockerError(Exception):
    pass


def binary():
    return os.environ.get("SAMPO_DOCKER") or "docker"


def docker(*args, stdin=None, timeout=300):
    """(exit code, stdout+stderr). DockerError when the binary is missing or docker hangs past `timeout`."""
    try:
        p = subprocess.run([binary(), *args], input=stdin, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except FileNotFoundError:
        raise DockerError(f"docker is not installed here ({binary()} not found)") from None
    except subprocess.TimeoutExpired:
        raise DockerError(f"docker {args[0]} took over {timeout} s") from None
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def unavailable():
    """Why docker can't be used (not installed, daemon down), or None."""
    try:
        code, out = docker("info", timeout=30)
    except DockerError as e:
        return str(e)
    return None if code == 0 else f"the docker daemon doesn't answer: {out.strip()[:200]}"


def free_gb(path=None):
    """Free disk in GB where docker keeps its images (/var/lib/docker when it exists, else /)."""
    path = path or ("/var/lib/docker" if os.access("/var/lib/docker", os.R_OK) else "/")
    return shutil.disk_usage(path).free / 1e9


def has_image(image):
    return docker("image", "inspect", image, timeout=60)[0] == 0


def prune():
    """Remove every container whose name starts with PREFIX, and nothing else."""
    code, out = docker("ps", "-aq", "--filter", f"name=^{PREFIX}", timeout=60)
    ids = out.split() if code == 0 else []
    if ids:
        docker("rm", "-f", *ids, timeout=120)
    return len(ids)


def _ours(tag):
    if not tag.startswith(PREFIX):
        raise ValueError(f"{tag!r}: the bench only builds and removes images named {PREFIX}*")
    return tag


def build(tag, context, timeout=1800):
    """T-3747: build image `tag` (sampo-bench-*) from a folder; DockerError when it fails or takes too long."""
    code, out = docker("build", "-t", _ours(tag), context, timeout=timeout)
    if code:
        raise DockerError(f"docker build {tag} failed ({code}): {out.strip()[-500:]}")


def rmi(tag):
    """T-3747: remove image `tag` (sampo-bench-* only); its exit code."""
    return docker("rmi", "-f", _ours(tag), timeout=120)[0]


def cap(text, n=OUT_CAP):
    return text if len(text) <= n else text[:n // 2] + f"\n… ({len(text) - n} characters left out) …\n" + text[-n // 2:]


class Container:
    """with Container(name, image) as c: c.edit(...) — started on enter, force-removed on exit."""

    def __init__(self, name, image, workdir="/testbed", prefix=ACTIVATE, run_seconds=RUN_SECONDS):
        if not name.startswith(PREFIX):
            name = PREFIX + name
        self.name, self.image, self.workdir, self.prefix = f"{name}-{uuid.uuid4().hex[:6]}", image, workdir, prefix
        self.run_seconds = run_seconds
        self.up = False

    def __enter__(self):
        code, out = docker("run", "-d", "--name", self.name, "-w", self.workdir, self.image, "sleep", "infinity",
                           timeout=120)
        if code:
            raise DockerError(f"can't start {self.image}: {out.strip()[:300]}")
        self.up = True
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self):
        if self.up:
            docker("rm", "-f", self.name, timeout=120)
            self.up = False

    def exec(self, cmd, stdin=None, seconds=None):
        """(code, output) of `bash -c cmd` in the workdir; code None on the time limit. Not shell-quoted by us:
        the command is the model's, and it runs inside the container."""
        seconds = seconds or self.run_seconds
        try:
            code, out = docker("exec", "-i", "-w", self.workdir, self.name, "timeout", "-k", "5", str(seconds),
                               "bash", "-c", self.prefix + cmd, stdin=stdin, timeout=seconds + 30)
        except DockerError:
            return None, ""
        return (None, out) if code == 124 else (code, out)

    # -- the tools, result text as Smith's own
    def rel(self, path):
        """The path inside the workdir, or None when it leaves it (an absolute path elsewhere, .., Git's own files)."""
        p = path.strip()
        if p.startswith(self.workdir + "/"):
            p = p[len(self.workdir) + 1:]
        if not p or p.startswith(("/", "-")) or ".." in p.split("/") or p.split("/")[0] == ".git" or "\0" in p:
            return None
        return p

    def read(self, path, start=1, lines=READ_LINES):
        rel = self.rel(path)
        if rel is None:
            return f"refused: {path}: outside the project"
        q, start, lines = shlex.quote(rel), max(start, 1), min(max(lines, 1), 400)
        if self.exec(f"test -d {q}", seconds=30)[0] == 0:
            return cap(self.exec(f"ls -1p {q} | head -300", seconds=30)[1])
        code, out = self.exec(f"test -f {q} && sed -n '{start},{start + lines}p' {q}", seconds=30)
        if code:
            return f"refused: {path} is not a regular file"
        rows = out.split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        shown = [f"{n:>5}  {row.rstrip()[:LINE_MAX]}" for n, row in enumerate(rows[:lines], start)]
        if len(rows) > lines:
            shown.append(f"… (more after line {start + lines - 1}; read on with start={start + lines})")
        return "\n".join(shown) or "(empty, or no lines there)"

    def search(self, pattern, glob=None):
        code, out = self.exec(f"git grep -n -I -E -e {shlex.quote(pattern)} -- {shlex.quote(glob or '.')} | head -80",
                              seconds=60)
        return "no match" if not out.strip() else "\n".join(row[:LINE_MAX] for row in out.splitlines())

    def fetch(self, rel):
        code, out = self.exec(f"test -f {shlex.quote(rel)} && cat {shlex.quote(rel)}", seconds=30)
        return out if code == 0 else None

    def store(self, rel, text):
        d = os.path.dirname(rel)
        code, out = self.exec((f"mkdir -p {shlex.quote(d)} && " if d else "") + f"cat > {shlex.quote(rel)}",
                              stdin=text, seconds=60)
        return code == 0, out

    def copy_in(self, src, dest):
        """T-3747: docker cp a host file or folder to `dest` in the container; OSError when it fails."""
        code, out = docker("cp", src, f"{self.name}:{dest}", timeout=120)
        if code:
            raise OSError(f"copy to {dest}: {out.strip()[-200:]}")

    def edit(self, path, old, new):
        from sampo import smith
        rel = self.rel(path)
        if rel is None:
            return f"refused: {path}: outside the project"
        have = self.fetch(rel)
        if have is None:
            return f"refused: {path} is not a file here"
        if not old:
            return "refused: old is empty; write replaces a whole file"
        mark = smith.elided(new, have)
        if mark:
            return f"refused: the new text elides code ({mark!r}): give the whole text it stands for; nothing changed"
        n = have.count(old)
        if n != 1:
            return (f"refused: old matches {'nowhere' if not n else f'{n} places'} exactly: " + (
                "copy it from a read, whitespace included" if not n else "include more lines around it")
                + ("; it matches once if whitespace is evened out: copy the lines exactly"
                   if isinstance(smith.near_anchor(have, old), tuple) else ""))
        ok, out = self.store(rel, have.replace(old, new, 1))
        return "edited" if ok else f"error: {out.strip()[:300]}"

    def write(self, path, content):
        from sampo import smith
        rel = self.rel(path)
        if rel is None:
            return f"refused: {path}: outside the project"
        have = self.fetch(rel) or ""
        mark = smith.elided(content, have)
        if mark:
            return f"refused: the content elides code ({mark!r}): write the whole file; nothing changed"
        ok, out = self.store(rel, content)
        return f"wrote {path} ({len(content)} characters)" if ok else f"error: {out.strip()[:300]}"

    def run(self, cmd):
        code, out = self.exec(cmd)
        return (f"timed out after {self.run_seconds} s" if code is None else f"exit {code}") + \
            (f"\n{cap(out)}" if out else "")
