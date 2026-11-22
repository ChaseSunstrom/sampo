"""T-5133 (child h9 of T-4316): which yardstick task kinds the calling cards serve. A card.json may carry "kinds", a list
of suite:kind strings (exam:bug, exam:vuln, exam:none, cases:TYPE); carded_kinds(root) is their union over every loaded
card (Sampo's own, the admitted store, a SAMPO_CALLCARDS folder). A card whose kinds entry is no suite:kind is left out
whole, and report() says why. Pure: it reads card.json through callcards.cards and writes nothing."""
import re

from sampo import callcards

EXAM_KINDS = ("bug", "vuln", "none")  # the exam suite's task kinds
CASES_TYPE = re.compile(r"[a-z][a-z0-9_-]{0,30}")  # a cases suite's TYPE: one lowercase word


def kind_problem(entry):
    """Why entry is no suite:kind, or None when it is one."""
    if not isinstance(entry, str):
        return f"{entry!r} is not a string"
    suite, sep, kind = entry.partition(":")
    if not sep or not suite or not kind:
        return f"{entry!r} is not suite:kind"
    if suite == "exam":
        return None if kind in EXAM_KINDS else f"{entry!r}: exam kinds are " + ", ".join(EXAM_KINDS)
    if suite == "cases":
        return None if CASES_TYPE.fullmatch(kind) else f"{entry!r}: a cases TYPE is a lowercase word"
    return f"{entry!r}: the suite is exam or cases"


def card_kinds(spec):
    """(kinds, why): a card's kinds, or ([], the reason) when its entry is not a list of suite:kind; no kinds is ([], None)."""
    if "kinds" not in spec:
        return [], None
    kinds = spec["kinds"]
    if not isinstance(kinds, list):
        return [], "kinds is not a list of suite:kind strings"
    for entry in kinds:
        why = kind_problem(entry)
        if why:
            return [], f"kinds entry {why}"
    return sorted(set(kinds)), None


def report(root=None):
    """{by_kind: {suite:kind: [card names]}, left_out: {card name: why its kinds count for nothing}} over the loaded cards."""
    found, _broken = callcards.cards(root)
    by_kind, left_out = {}, {}
    for name in sorted(found):
        kinds, why = card_kinds(found[name])
        if why:
            left_out[name] = why
        for k in kinds:
            by_kind.setdefault(k, []).append(name)
    return {"by_kind": by_kind, "left_out": left_out}


def carded_kinds(root=None):
    """The set of suite:kind some loaded card names."""
    return set(report(root)["by_kind"])
