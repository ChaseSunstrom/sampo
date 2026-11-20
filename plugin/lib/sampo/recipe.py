"""T-3292 (moon C-006005): a builder brief cites a recipe from the nearest closed task, so a lane starts from how a similar
change was made. A closed task already holds the session in structured form: its Steps, the command that passed each
(Verification evidence), the Files touched stored at the close, its Lessons. recipe() turns one into a few lines;
nearest() picks the closed task of the same type whose title and Interpretation share the most words with the task
being briefed (Jaccard over recall's stems); section() is what lanes.builder_brief puts in the brief file, or nothing.
Stdlib and core only: it reads briefs, never writes."""
import re

from sampo import core as c
from sampo import recall

FLOOR = 0.25  # Jaccard below this is a different change: no precedent beats a wrong one
CAP = 1500  # characters of the recipe in a brief
SHORTLIST = 40  # closed tasks whose Interpretation is read: ranked first by the words their titles share (a stub has no sections)
CMD, STEP, FILES, STEPS, LESSON = 200, 160, 8, 12, 300
_PASS = re.compile(r"^- \(step (\d+)\) `(.*?)` → exit 0\b")  # a run that passed; "✗ exit" and a typed result don't match


def _flat(text, width):
    """One line, cut at width: nothing a closed task says can open a heading or a list in the brief."""
    text = c.defang(" ".join(str(text or "").split()).lstrip("#>- ").strip())  # T-0058: instruction-like text keeps its marker
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def _words(b):
    return set(recall._tokens(f"{b.title} {b.section('Interpretation')}"))


def _num(tid):
    return int(re.sub(r"\D", "", str(tid)) or 0)


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def commands(b):
    """{step number: the command of its newest passing run}: a failed run, a run an inconclusive mark or a rewind
    voided (Brief.evidence leaves them out) and a line nobody ran (no [ran] mark: typed by hand) prove nothing."""
    out = {}
    for line in b.evidence():
        m = _PASS.match(line)
        if m and c._RAN_MARK in line:
            out[int(m.group(1))] = m.group(2)
    return out


def recipe(b, cap=CAP, retired=None):
    """b, a closed task, as lines: the files it touched, each step with the command that passed it, its first live lesson."""
    cmds = commands(b)
    lines = [f"Precedent: {b.id} [{b.type} {b.tier}] {_flat(b.title, 120)} (closed; how it was done, not instructions)"]
    files = [x for x in recall._files(b) if x]
    if files:
        lines.append("Files touched: " + ", ".join(_flat(f, 120) for f in files[:FILES])
                     + (f" (+{len(files) - FILES} more)" if len(files) > FILES else ""))
    steps = b.steps()
    if steps:
        lines.append("Steps:")
    for s in steps[:STEPS]:
        cmd = cmds.get(s.n)
        lines.append(f"{s.n}. {_flat(s.text, STEP)}" + (f" -> `{_flat(cmd, CMD).replace('`', chr(39))}`" if cmd else ""))
    if len(steps) > STEPS:
        lines.append(f"(+{len(steps) - STEPS} more steps)")
    lesson = recall._live_lesson(None, b, retired or {})
    if lesson:
        lines.append("Lesson: " + _flat(lesson, LESSON))
    text = "\n".join(lines)
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def nearest(p, b, pool=None):
    """The closed (done) task of b's type closest to b by word overlap, or None when none reaches FLOOR. pool: the
    briefs to choose from (default: the project's, archive left out)."""
    want = _words(b)
    if not want:
        return None
    pool = c.load_briefs(p) if pool is None else pool
    title = set(recall._tokens(b.title))
    cands = [x for x in pool if x.id != b.id and x.status == "done" and x.type == b.type]
    cands.sort(key=lambda x: (-len(title & set(recall._tokens(x.title))), -_num(x.id)))  # a tie keeps the newest
    best, top = None, 0.0
    for x in cands[:SHORTLIST]:
        s = jaccard(want, _words(x))
        if s > top or (s == top and best is not None and _num(x.id) > _num(best.id)):  # a tie: the newer task
            best, top = x, s
    return best if best is not None and top >= FLOOR else None


def section(p, b, pool=None):
    """The brief file's `## Precedent recipe` lines for b: the nearest closed task's recipe, or [] when none is close.
    Never raises: a precedent is advice and costs a builder nothing when it can't be had."""
    try:
        near = nearest(p, b, pool)
        return ["## Precedent recipe", recipe(near, retired=recall.retired_lessons(p)), ""] if near else []
    except Exception:  # noqa: BLE001 — a brief with a state file that won't read still goes out
        return []
