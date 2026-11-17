"""pick-function's checker: whatever the model says, the name must be a class, function or method the file defines.
Which one is right is the model's judgement; that it exists is proof."""
import difflib

from cardkit import defs, fail, parse, read_input, source

inp = read_input()
facts, path = inp["facts"], inp["inputs"]["path"]
if not isinstance(facts, dict) or not isinstance(facts.get("name"), str):
    fail("the answer isn't an object with a name")
names = sorted({q for q, _, _ in defs(parse(source(path), path))})
if facts["name"] not in names:
    near = difflib.get_close_matches(facts["name"], names, n=3, cutoff=0.0)
    fail(f"{path} defines no {facts['name']!r}" + (f": it has {', '.join(map(repr, near))}" if near else " (it has no definitions)"))
