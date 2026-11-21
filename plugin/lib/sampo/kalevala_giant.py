"""T-5014 (a slice of T-4337): a Kalevala generation is a GIANT task. `kalevala.gen` records the generation, then calls
giant_task here (after its own lock is released: the project lock isn't reentrant, and cli.mutate takes it again). The
task's Design is the gen's setup, its Breakdown has one XL milestone per tower the gen sets, its Integration criterion is
the gen's own judgement (`sampo kalevala select N`), and its three plan sections (what big.task_plan's skeleton
holds empty) are filled in the write that makes the task, so `sampo big check` passes at once. Running the milestones is `sampo big expand` then `sampo big run`; this module never
runs a tower and never calls a model."""
from sampo import core as c

MARK = "kalevala_gen"  # the brief's meta key that says which generation a GIANT belongs to
SOURCE = "self"


def find(p, n):
    """The GIANT already made for gen n (its mark is on its first write), or None."""
    for b in c.load_briefs(p):
        if str(b.meta.get(MARK)) == str(n):  # front matter reads back as text
            return b
    return None


def _key(i):
    return f"t{i}"


def _design(n, rows):
    lines = [f"Gen: {n}",
             f"Setup: gen-{n}/setup.md in Kalevala's home (sampo kalevala gen wrote it); the towers below are its set.",
             "decision: one XL milestone per tower; each works its own aspect in its own tower home, all at once",
             f"decision: the generation is judged once, by sampo kalevala select {n}, from the towers' exam lines"]
    lines += [f"interface: tower {r['name']} works {c.plain(str(r.get('aspect') or r['name']))}"
              f" (tower gen {r.get('generation')}), run as sampo genesis --tower {r['name']}" for r in rows]
    return "\n".join(lines) + "\n"


def _breakdown(rows):
    out = []
    for i, r in enumerate(rows, 1):
        aspect = c.plain(str(r.get("aspect") or r["name"]))
        out.append(f"[{_key(i)}] Tower {r['name']} works {aspect}\ntier: XL\n"
                   f"outcome: tower {r['name']} has run its next steps for this gen and its exam lines are graded\n")
    return "\n".join(out)


def _integration(n):
    return (f"criterion: gen {n} is judged against its parent by the towers' combined exam results"
            f" :: sampo kalevala select {n}\ncovers: 1\n")


def giant_task(p, n, setup_rows):
    """The GIANT task for Kalevala gen n: one milestone per row of setup_rows ({"name", "aspect", "generation"}).
    Idempotent: a gen that has its task already (found by its mark) returns it. Raises ValueError on no rows. Call it
    with no project lock held."""
    from sampo import cli
    if not setup_rows:
        raise ValueError("a generation with no tower has no milestone to plan")
    towers = ", ".join(r["name"] for r in setup_rows)
    title = f"Kalevala gen {n}: work every tower's aspect"
    raw = (f"Kalevala generation {n} (sampo kalevala gen): the towers {towers} each work their own aspect, then the "
           f"generation is judged against its parent (sampo kalevala select {n}).")
    with c.lock(p.dir):
        if (b := find(p, n)) is not None:  # looked for under the lock: two callers never both make one
            return b
        b = cli._create(p, title, "FEATURE", "GIANT", "planned", raw=raw, source=SOURCE,
                        meta={MARK: n})  # the mark is on the first write: a rerun after a kill finds it
        b.set_section("Interpretation",
                      f"Reading 1: the generation is one GIANT task, a milestone per tower, run by sampo big run; it is "
                      f"judged by sampo kalevala select {n}. Reading 2: gen runs the towers itself; ruled out, because "
                      f"sampo kalevala step and sampo big run already drive them.\n")
        b.set_section("Risks and rollback",
                      f"A tower's step can fail or spend its share: sampo kalevala watch names it, pause stops it. "
                      f"Select reverts a gen that does not beat its parent (seed base back to the parent's); drop this "
                      f"task to abandon gen {n}'s plan, nothing in a tower is undone by it.\n")
        b.add_ac(f"gen {n} is judged against its parent by the towers' exam results", f"sampo kalevala select {n}")
        b.add_step(f"expand the milestones and run them: sampo big expand {b.id}, then sampo big run {b.id}")
        # the three plan sections big.task_plan would write as an empty skeleton, filled from the setup in the same
        # write that makes the task: no half-made GIANT is ever on disk for a rerun to find
        b.set_section("Design", _design(n, setup_rows))
        b.set_section("Breakdown", _breakdown(setup_rows))
        b.set_section("Integration", _integration(n))
        b.append_log(f"kalevala gen {n}: plan written from the setup ({len(setup_rows)} milestone(s))")
        c.save_brief(p, b, touch=False)
        c.log_event(p, "kalevala_giant", task=b.id, data={"gen": n, "towers": [r["name"] for r in setup_rows]},
                    session=cli.session())
    c.regen_views(p)
    return b
