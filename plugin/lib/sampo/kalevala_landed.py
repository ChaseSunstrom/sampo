"""T-5025 (slice of T-4337, a generation is a GIANT task): what landed in a Kalevala generation and what each item did.

kalevala.select already holds every tower's closed-scenario count for gen N and for gen N-1. landed() reads the children
of the gen's GIANT task (the id in the gen's manifest entry, key `giant`) and pairs each with its tower's score delta:
no exam is re-run, nothing is written, nothing is judged here. A child names its tower with a `tower:` line in its
brief's frontmatter; without one, the first tower of the gen whose name the child's title holds. render() and
lineage_lines() turn the reading into the Landed section of select.md and the lines under a gen in lineage.md.
Nothing here gates anything: the verdict is select's, from the scores."""
import re

from sampo import big
from sampo import core as c

MAX_ROWS = 200  # the children one section lists (big.MAX_BLOCKS is the plan's own cap); the rest are counted
TITLE = 80


def _title(s):
    return c.fit(" ".join(c.plain(s or "").split()), TITLE)


def _tower(g, kid):
    """The tower a child is measured by: its `tower:` meta, else the first of the gen's towers its title names, or None."""
    named = str(kid.meta.get("tower") or "").strip()
    if named:
        return c.plain(named)[:40]
    title = (kid.title or "").lower()
    return next((t for t in g.get("set", []) if t.lower() in title), None)


def _effect(g, tower, scores, prev):
    """(before, after, delta, why): the score of the child's tower in the earlier gen and this one, their difference, and
    the reason there is none (delta None)."""
    if prev is None:
        return None, None, None, "no earlier gen to compare"
    if not tower:
        return None, None, None, "no tower named"
    before, after = (prev.get("scores") or {}).get(tower), scores.get(tower)
    if before is None or after is None:
        return before, after, None, f"{tower} did not score both gens"
    return before, after, after - before, ""


def landed(p, g, scores, prev):
    """{"giant": id or None, "note": why there are no rows or "", "rows": [{"id", "status", "title", "tower", "before",
    "after", "delta", "why"}]}: every child of gen g's GIANT task in the plan's order. `scores` is {tower: closed count or
    None} for this gen; `prev` is the manifest entry of gen n-1 (None for gen 1). Pure: reads briefs, writes nothing."""
    gid = g.get("giant")
    if not isinstance(gid, str) or not gid.strip():
        return {"giant": None, "note": "no GIANT task recorded", "rows": []}
    gid = c.plain(gid.strip())[:20]
    briefs = {b.id: b for b in c.load_briefs(p, include_archive=True)}
    parent = briefs.get(gid)
    if parent is None:
        return {"giant": gid, "note": f"GIANT {gid} has no brief", "rows": []}
    rows = []
    for _key, (kid_id, _sig) in big.kids_of(parent).items():
        kid = briefs.get(kid_id)
        if kid is None:
            rows.append({"id": kid_id, "status": "missing", "title": "", "tower": None, "before": None, "after": None,
                         "delta": None, "why": "its brief is gone"})
            continue
        tower = _tower(g, kid)
        before, after, delta, why = _effect(g, tower, scores, prev)
        rows.append({"id": kid.id, "status": c.plain(str(kid.status)), "title": _title(kid.title), "tower": tower,
                     "before": before, "after": after, "delta": delta, "why": why})
    return {"giant": gid, "note": "" if rows else f"GIANT {gid} has no children yet", "rows": rows}


def _line(r):
    head = f"{r['id']} [{r['status']}]" + (f" {r['title']}" if r["title"] else "")
    if r["delta"] is None:
        return f"{head}: no delta ({r['why']})"
    return f"{head}: {r['tower']} {r['before']} -> {r['after']} ({r['delta']:+d})"


def _lines(res):
    out = [_line(r) for r in res["rows"][:MAX_ROWS]]
    if len(res["rows"]) > MAX_ROWS:
        out.append(f"... and {len(res['rows']) - MAX_ROWS} more")
    return out


def render(n, res):
    """The Landed section of gen n's select.md."""
    if not res["rows"]:
        return f"\nLanded: {res['note']} for gen {n}\n"
    return f"\n## Landed\n\nGIANT: {res['giant']}\n" + "".join(f"- {x}\n" for x in _lines(res))


def lineage_lines(n, res):
    """What lineage.md adds under gen n's heading: one `- landed` bullet, each child a sub-bullet."""
    if not res["rows"]:
        return f"- landed: {res['note']} for gen {n}\n"
    return f"- landed (GIANT {res['giant']}):\n" + "".join(f"  - {x}\n" for x in _lines(res))
