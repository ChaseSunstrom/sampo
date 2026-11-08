"""T-3321 (Fleet part 1): cross-machine claims on git refs. A claim is the ref refs/sampo/<slug>/claim/<key> (key: a
task id, tower:<name>, kgen:<N>; ':' is written %3A) pointing at an empty-tree commit whose message is JSON
{key, machine, name?, session, at, until, fingerprint}. The slug is the repo's root commit, so every clone agrees on it.
Every write is `git push --atomic --force-with-lease=<ref>:<old-or-empty>`, so the remote picks exactly one winner among
racers; no leader, any git remote works. A claim is expired only when its body parsed and until + 5 min slack has passed
on the taker's clock, and the writer's `at` is not more than the slack ahead of the taker's clock (a writer far ahead
may have a skewed clock: its claim is not taken). An unreadable holder is never taken over. Takeover sets the lease to
the old sha, so exactly one taker wins. Renewals of all of a machine's claims go in one atomic push, at most every 10
minutes. Remote: FOREMAN_CLAIMS_REMOTE, else origin. Results: taken|held|lost (the race was lost)|offline (remote
unreachable: the caller decides)|error (the remote refused or git failed). Nothing here raises past take/release/renew/list."""
import datetime
import hashlib
import json
import os
import re
import subprocess
import time

TTL = 30 * 60
SLACK = 5 * 60
RENEW_EVERY = 10 * 60
GIT_TIMEOUT = 15  # hooks call this
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
_ENV = {"GIT_AUTHOR_NAME": "sampo", "GIT_AUTHOR_EMAIL": "sampo@localhost",
        "GIT_COMMITTER_NAME": "sampo", "GIT_COMMITTER_EMAIL": "sampo@localhost", "GIT_TERMINAL_PROMPT": "0"}


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def expired(body, now):
    """A parsed claim body whose until + slack has passed, from a writer not far ahead of this clock."""
    until, at = _epoch((body or {}).get("until")), _epoch((body or {}).get("at"))
    if until is None or now <= until + SLACK:
        return False
    return not (at is not None and at > now + SLACK)


def _failed():
    return subprocess.CompletedProcess([], 255, "", "git did not run")


ADVISORY = ("intent", "plugin")  # shown by fleet, never refuse: they differ on the first machine of a rolling upgrade


def fingerprint(parts):
    """T-3334: a short hash over the enforced drift parts ('' when there are none); derived, never stored apart."""
    own = {k: v for k, v in (parts or {}).items() if k not in ADVISORY}
    return hashlib.sha256(json.dumps(own, sort_keys=True).encode()).hexdigest()[:12] if own else ""


def differs(mine, theirs, advisory=False):
    """The enforced parts that differ between two parts dicts (the advisory ones with advisory=True)."""
    return sorted(k for k in set(mine) | set(theirs) if (k in ADVISORY) == advisory and mine.get(k) != theirs.get(k))


class Claims:
    def __init__(self, root, slug=None, remote=None, session=None, parts=None):
        self.parts = parts or {}  # T-3334: the drift fingerprint's parts (mandate.parts); none = no fingerprint
        self.root = root
        self.remote = remote or os.environ.get("FOREMAN_CLAIMS_REMOTE") or "origin"
        from sampo import eco
        self.me = eco.machine()
        self.slug = slug or self._root_slug()
        # stable per machine and project: a CLI call each time is still the same holder
        self.session = session or os.environ.get("CLAUDE_CODE_SESSION_ID") or f"{self.me['id']}-{self.slug[:8]}"

    def _root_slug(self):
        r = self._git("rev-list", "--max-parents=0", "HEAD")
        roots = sorted(r.stdout.split()) if r.returncode == 0 else []
        return roots[0][:16] if roots else "noroot"  # ponytail: a repo without commits shares one namespace per remote

    def ref(self, key):
        if not _KEY.fullmatch(key or "") or ".." in key or key.endswith((".", ".lock")):
            raise ValueError(f"not a claim key: {key!r}")
        return f"refs/sampo/{self.slug}/claim/{key.replace(':', '%3A')}"

    def _git(self, *a, input=None):
        try:
            return subprocess.run(["git", *a], cwd=self.root, capture_output=True, text=True, input=input,
                                  timeout=GIT_TIMEOUT, env=dict(os.environ, **_ENV))
        except (OSError, subprocess.SubprocessError):
            return _failed()

    def _remote_refs(self, pats=None):
        """{ref: sha} on the remote (the claim refs, or just pats), or None when it can't be reached."""
        r = self._git("ls-remote", "--", self.remote, *(pats or [f"refs/sampo/{self.slug}/claim/*"]))
        if r.returncode:
            return None
        return {p[1]: p[0] for p in (ln.split() for ln in r.stdout.splitlines()) if len(p) == 2}

    def _body(self, ref, sha):
        """The claim's JSON, or None when it can't be read (fetch failed, not JSON): never read as expired."""
        if self._git("cat-file", "-e", sha).returncode and self._git("fetch", "--quiet", "--", self.remote, f"+{ref}:{ref}").returncode:
            return None
        try:
            data = json.loads(self._git("log", "-1", "--format=%B", sha).stdout)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _commit(self, key, now):
        body = {"key": key, "machine": self.me["id"], "session": self.session, "at": iso(now),
                "until": iso(now + TTL), "fingerprint": fingerprint(self.parts), "parts": self.parts,
                "n": os.urandom(4).hex()}
        if self.me["named"]:  # a hostname is never written unasked
            body["name"] = self.me["name"]
        self._git("hash-object", "-t", "tree", "-w", "--stdin", input="")
        r = self._git("commit-tree", EMPTY_TREE, "-m", json.dumps(body))
        return r.stdout.strip() if r.returncode == 0 else None

    def _push(self, updates):
        """updates: [(ref, old_sha_or_'', new_sha_or_None to delete)] in one atomic push: ok|lost|offline|error.
        lost only when a re-read shows a ref moved off old; a refusing remote is an error."""
        args = ["push", "--atomic", "--quiet"]
        args += [f"--force-with-lease={ref}:{old}" for ref, old, _ in updates]
        args += ["--", self.remote] + [f"{new or ''}:{ref}" for ref, _, new in updates]
        if self._git(*args).returncode == 0:
            return "ok"
        refs = self._remote_refs([ref for ref, _, _ in updates])  # the refs pushed, not just claim/*
        if refs is None:
            return "offline"
        return "lost" if any(refs.get(ref, "") != old for ref, old, _ in updates) else "error"

    def _mine_now(self, by):
        return bool(by) and by.get("machine") == self.me["id"] and by.get("session") == self.session

    def take(self, key, now=None):
        """{result: taken|held|lost|offline|error, by?, sha?}. Taking your own live claim renews it."""
        now = time.time() if now is None else now
        ref = self.ref(key)
        refs = self._remote_refs()
        if refs is None:
            return {"result": "offline"}
        old = refs.get(ref, "")
        if key.startswith("tower:") and self.parts:  # T-3334: a drifted machine works no tower
            d = self.drift(now, refs)
            if d:
                return {"result": "drift", "differs": d}
        if old:
            by = self._body(ref, old)
            if not self._mine_now(by) and not expired(by, now):
                return {"result": "held", "by": by or {}}
        new = self._commit(key, now)
        if not new:
            return {"result": "error"}
        st = self._push([(ref, old, new)])
        return {"result": {"ok": "taken"}.get(st, st), "sha": new}

    def release(self, key):
        ref = self.ref(key)
        refs = self._remote_refs()
        if refs is None:
            return {"result": "offline"}
        old = refs.get(ref, "")
        if not old:
            return {"result": "released"}
        by = self._body(ref, old)
        if not self._mine_now(by):
            return {"result": "held", "by": by or {}}
        st = self._push([(ref, old, None)])
        return {"result": {"ok": "released"}.get(st, st)}

    def reserve_ids(self, floor=1, size=32, deadline=5.0):
        """T-3329: reserve T-numbers [start, start+size) on refs/sampo/<slug>/ids, a commit whose message is
        {"next": N}; a compare-and-swap push, so no two machines get one block. start >= floor. None offline, or
        after 8 lost races or `deadline` seconds. An unreadable counter is never reset: None."""
        ref = f"refs/sampo/{self.slug}/ids"
        end = time.time() + deadline
        for _ in range(8):
            if time.time() > end:
                return None
            r = self._git("ls-remote", "--", self.remote, ref)
            if r.returncode:
                return None
            old = (r.stdout.split() or [""])[0]
            nxt = (self._body(ref, old) or {}).get("next") if old else 1
            if not isinstance(nxt, int):
                return None
            start = max(nxt, floor)
            self._git("hash-object", "-t", "tree", "-w", "--stdin", input="")
            c = self._git("commit-tree", EMPTY_TREE, "-m", json.dumps({"next": start + size, "n": os.urandom(4).hex()}))
            if c.returncode:
                return None
            st = self._push([(ref, old, c.stdout.strip())])
            if st == "ok":
                return start
            if st != "lost":
                return None  # offline, or a remote that refuses: more rounds won't help
            time.sleep(0.05 + os.urandom(1)[0] / 2560)  # jittered, so two racers don't collide again in step
        return None

    def _stamp(self):
        return self._git("rev-parse", "--git-path", "sampo-claims-renewed").stdout.strip()

    def renew(self, now=None, force=False):
        """One atomic push extending every live claim this session holds, at most every 10 minutes (force: now).
        {result: renewed|skipped|offline|error, keys}."""
        now = time.time() if now is None else now
        stamp = os.path.join(self.root, self._stamp())
        try:
            with open(stamp) as f:
                last = float(f.read())
        except (OSError, ValueError):
            last = None
        if not force and last is not None and 0 <= now - last < RENEW_EVERY:
            return {"result": "skipped", "keys": []}
        rows = self.list(now=now)
        if rows is None:
            return {"result": "offline", "keys": []}
        rows = [r for r in rows if r["machine"] == self.me["id"] and r["session"] == self.session and not r["expired"]]
        ups = [(self.ref(r["key"]), r["sha"], self._commit(r["key"], now)) for r in rows]
        if not all(u[2] for u in ups):
            return {"result": "error", "keys": []}
        st = self._push(ups) if ups else "ok"
        if st == "lost":  # one claim moved under us: renew the rest one by one, the lost ones drop out
            kept = []
            for up, r in zip(ups, rows):
                one = self._push([up])
                if one == "ok":
                    kept.append(r)
                elif one != "lost":
                    return {"result": one, "keys": []}
            rows = kept
        elif st != "ok":
            return {"result": st, "keys": []}
        try:
            with open(stamp, "w") as f:
                f.write(str(now))
        except OSError:
            pass
        return {"result": "renewed", "keys": [r["key"] for r in rows]}

    def list(self, now=None, refs=None):
        """Every claim on the remote [{key, machine, name, session, at, until, fingerprint, parts, expired, sha}], or
        None offline. refs: the {ref: sha} the caller just read, so a second ls-remote is skipped."""
        now = time.time() if now is None else now
        refs = self._remote_refs() if refs is None else refs
        if refs is None:
            return None
        pre = f"refs/sampo/{self.slug}/claim/"
        if refs:  # one fetch for all of them
            self._git("fetch", "--quiet", "--", self.remote, *[f"+{r}:{r}" for r in refs])
        out = []
        for ref, sha in sorted(refs.items()):
            b = self._body(ref, sha) or {}
            out.append({"key": b.get("key") or ref[len(pre):].replace("%3A", ":"), "machine": b.get("machine"),
                        "name": b.get("name"), "session": b.get("session"), "at": b.get("at"),
                        "until": b.get("until"), "fingerprint": fingerprint(b.get("parts")),
                        "parts": b.get("parts") if isinstance(b.get("parts"), dict) else {},
                        "expired": expired(b, now), "sha": sha})
        return out

    def drift(self, now=None, refs=None):
        """T-3334: the enforced parts of ours that differ from the fleet's, [] when none. One vote per other machine
        (its newest live claim); the fleet's is the most common fingerprint. A tie that includes ours is no drift."""
        newest = {}
        for r in self.list(now=now, refs=refs) or []:
            who = (r["machine"], r["session"])
            if r["expired"] or who == (self.me["id"], self.session) or not r["parts"]:
                continue
            if who not in newest or (r["at"] or "") > (newest[who]["at"] or ""):
                newest[who] = r
        votes = {}
        for r in newest.values():
            votes.setdefault(r["fingerprint"], []).append(r)
        if not votes:
            return []
        top = max(len(v) for v in votes.values())
        mine = fingerprint(self.parts)
        if len(votes.get(mine, [])) == top:
            return []
        best = min((v[0] for v in votes.values() if len(v) == top), key=lambda r: r["fingerprint"])
        return differs(self.parts, best["parts"])

    def fleet(self, now=None):
        """Who holds what, from the refs: [{key, machine, who, until, expired, match, differs}], or None offline.
        `who` is the name the machine chose, else its id (never a hostname); `differs` names the parts that differ."""
        rows = self.list(now=now)
        if rows is None:
            return None
        mine = fingerprint(self.parts)
        return [dict(r, who=r["name"] or r["machine"], match=r["fingerprint"] == mine,
                     differs=differs(self.parts, r["parts"]) if r["fingerprint"] != mine and r["parts"] else [],
                     advisory=differs(self.parts, r["parts"], True) if r["parts"] else [])
                for r in rows]


def cmd_fleet(args):
    """sampo fleet: every claim on the remote, who holds it, until when, and whether its fingerprint matches ours."""
    from sampo import cli, mandate
    p = cli.resolve(args)
    rows = Claims(p.root, parts=mandate.parts(p)).fleet()
    if rows is None:
        print("fleet: the claims remote can't be reached")
        return 0
    for r in rows:
        fp = ("no fingerprint" if not r["fingerprint"] else "matches" if r["match"] else "differs: " + ", ".join(r["differs"]))
        adv = f"  (advisory: {', '.join(r['advisory'])})" if r["advisory"] else ""
        print(f"{r['key']}  {r['who']}" + (f"  until {r['until']}" if r["until"] else "")
              + f"{' (expired)' if r['expired'] else ''}  {fp}{adv}")
    print(f"{len(rows)} claim(s)" if rows else "no claims")
    return 0
