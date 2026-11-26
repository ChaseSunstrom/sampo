# ANDON: T-6168 fix round 1: one edit blocked by a lease

**Question.** The review's class-wall finding (train.py ~2431-2434, inside `_run`) needs one edit, and
`plugin/lib/sampo/train.py::_run` is leased by another task (T-6300 / the T-6187 builder). The lease holder's
message says to leave `_run` alone. I could not take it. What is left in `_run` is the old load division.

**Assumption I proceed on.** I left the call site unchanged. `class_load` and `class_wall` are restored in
train.py (their original text), so `_run` still runs. The rest of the fix is in.

**The change still to make at `_run` (train.py, the `run1` loop):** replace

    lf = class_load()  # ...
    named = [w for w in str(line or "").split() if w.startswith("test_")]  # ...
    for w in named:
        classwall[w] = classwall.get(w, 0.0) + class_wall(row["s"] / len(named), lf)

with

    named = [w for w in str(line or "").split() if w.startswith("test_")]
    for w in named:  # each named class takes this run's raw wall
        classwall[w] = classwall.get(w, 0.0) + row["s"]

then delete `class_load` and `class_wall` from train.py and the `assertNotIn`-free test note in
`SpeedStanding.test_a_class_whose_wall_doubles_on_a_loaded_host_is_still_flagged`.

**What changes if wrong.** If the lease holder keeps `_run`, the class wall stays divided by load and by the
number of classes in a run. That leaves the review's edge and intent findings on train.py:2434 open.

---

# ANDON: T-5241 check_unwired has no census.json writer on this branch

Question: the reviewer is right. Nothing on this branch writes census.json with the "unwired" and "previous" keys. check_unwired (plugin/lib/sampo/doctor.py:602) reads them, so on this branch it returns PASS every time. The writer is the capture run from T-5239, which the CHANGELOG cites and which is not in this lane's history.

Assumption I proceed on: T-5239 lands the writer (it owns the capture run and the unwired count), and this lane only adds the read side. I made no code edit in this fix round, because I have no shell to run tests and no spec for the capture run's write path.

If the assumption is wrong: this lane must also write census.json. The write would go in the capture run, with the unwired count from wired.scan_full rows (len of the rows), "previous" set to the prior "unwired" value, and an atomic write. That needs a test first. Otherwise, merge T-5239 before T-5241, or add it as a Built on dependency.
>