#!/usr/bin/env python3
"""T-1537: the taste ledger for looks: a kept look, a reverted one and 'not like that' move its rank; a 12-pair
calibration picker records twelve answers; learning reads only the local files the user names."""
import builtins
import importlib.util
import io
import json
import os
import socket
import struct
import unittest
import zlib
from unittest import mock

from helpers import PLUGIN, ForemanTestCase, read_text  # puts plugin/lib on sys.path
from sampo import core as c
from sampo import taste

LOOKS = os.path.join(PLUGIN, "skills", "library", "cards", "looks")


class Opens:
    """Every path builtins.open and os.open are asked for while it's active."""

    def __init__(self):
        self.paths, self._open, self._os_open = [], builtins.open, os.open

    def __enter__(self):
        def op(f, *a, **kw):
            if isinstance(f, (str, bytes, os.PathLike)):
                self.paths.append(os.path.realpath(f))
            return self._open(f, *a, **kw)

        def os_op(f, *a, **kw):
            self.paths.append(os.path.realpath(f))
            return self._os_open(f, *a, **kw)
        self._patches = [mock.patch.object(builtins, "open", op), mock.patch.object(os, "open", os_op),
                         mock.patch.object(socket, "socket", side_effect=AssertionError("no network"))]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()

    def outside(self, *allowed):
        allowed = [os.path.realpath(a) for a in allowed]
        return [x for x in self.paths if not any(x == a or x.startswith(a + os.sep) for a in allowed)]


def png(w, h, rgb=(0, 0, 0), rows=True):
    """A PNG built with the stdlib: w×h of one colour, or (rows=False) only its header and an empty image stream."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h)) if rows else b""
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Ledger(ForemanTestCase):
    def test_a_kept_and_a_reverted_look_update_the_ledger(self):
        self.fm("looks", "keep", "look-lido")
        self.fm("looks", "keep", "Night market")  # a title works as well as the card's name
        ledger = os.path.join(c.state_dir(), "taste", "looks.json")
        events = json.loads(read_text(ledger))["events"]
        self.assertEqual([(e["signal"], e["look"]) for e in events],
                         [("kept", "look-lido"), ("kept", "look-night-market")])
        top = self.fm_json("looks")["looks"]
        self.assertEqual(len(top), 20, "every look is ranked, the unseen ones at 0")
        self.assertEqual({top[0]["look"], top[1]["look"]}, {"look-lido", "look-night-market"})
        self.fm("looks", "revert", "lido", "--why", "too cheerful for a bank")
        self.fm("looks", "not", "look-lido", "--why", "not like that")
        rank = {r["look"]: r for r in self.fm_json("looks")["looks"]}
        self.assertLess(rank["look-lido"]["score"], 0)
        self.assertEqual(rank["look-lido"]["kept"], 1)
        self.assertEqual(rank["look-lido"]["reverted"], 1)
        self.assertEqual(self.fm_json("looks")["looks"][0]["look"], "look-night-market")
        self.assertEqual(self.fm_json("looks")["looks"][-1]["look"], "look-lido")
        last = json.loads(read_text(ledger))["events"][-1]
        self.assertEqual((last["signal"], last["why"]), ("not", "not like that"))

    def test_an_unknown_look_or_signal_writes_nothing(self):
        p = self.fm("looks", "keep", "look-nope", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("look-lido", p.stderr, "names the looks there are")
        p = self.fm("looks", "keep", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(c.state_dir(), "taste", "looks.json")))
        with self.assertRaises(ValueError):
            taste.record("loved", "look-lido")
        p = self.fm("looks", "lido", check=False)  # a look where the verb goes: the verbs named, and the fix
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("invalid choice", p.stderr)
        for word in ("rank, keep, revert, not, calibrate, learn", "fm looks keep lido"):
            self.assertIn(word, p.stderr)

    def test_the_ledger_stays_bounded_and_survives_a_corrupt_file(self):
        path = taste.ledger_path()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("{not json")
        taste.record("kept", "look-lido")  # a broken ledger starts over rather than failing every call
        self.assertEqual(len(taste.load()["events"]), 1)
        self.assertEqual(read_text(path + ".bak"), "{not json", "the broken file is kept beside it first")
        odd = [{"signal": "kept", "look": ["x"]}, {"signal": "calibration", "pairs": [[1, {}]], "answers": "a"},
               {"signal": "calibration", "pairs": "ab"}, "junk", None]
        self.assertEqual(taste.scores(odd), {}, "a hand-edited ledger's odd rows are skipped, not a crash")
        with mock.patch.object(taste, "EVENTS_KEEP", 5):
            for _ in range(9):
                taste.record("not", "look-lab-notebook", why="x" * 5000)
            events = taste.load()["events"]
        self.assertEqual(len(events), 5)
        self.assertLessEqual(max(len(e.get("why", "")) for e in events), taste.WHY_CAP)

    def test_learned_matches_never_outweigh_what_the_user_kept(self):
        taste.record("kept", "look-observatory")
        for n in range(6):  # a glob of stylesheets, each matching Lido, one of them twice
            taste.record("learned", "look-lido", source=f"s{n}.css")
        taste.record("learned", "look-lido", source="s0.css")
        events = taste.load()["events"]
        self.assertEqual(sum(e["signal"] == "learned" for e in events), 6, "one learned per file name and look")
        rank = {r["look"]: r for r in taste.rank()}
        self.assertEqual(rank["look-lido"]["score"], taste.LEARNED_CAP)
        self.assertLessEqual(rank["look-lido"]["score"], rank["look-observatory"]["score"])
        self.assertEqual([r["look"] for r in taste.rank()][:2], ["look-observatory", "look-lido"], "kept wins a tie")
        with mock.patch.object(taste, "EVENTS_KEEP", 4):
            taste.record("not", "look-cartographer")
            kept = [e["signal"] for e in taste.load()["events"]]
        self.assertEqual(len(kept), 4)
        self.assertEqual([s for s in kept if s != "learned"], ["kept", "not"], "learned events are trimmed first")


class Calibration(ForemanTestCase):
    def test_pairs_show_every_look_for_up_to_twice_as_many_looks_as_pairs(self):
        for n in (20, 24):  # T-2045: 20 looks, and 24 where the offset n/2 is allowed
            known = {f"look-{i:02d}": {} for i in range(n)}
            rivals = set()
            for rnd in range(4):
                got = taste.pairs(known, rnd)
                self.assertEqual({x for p in got for x in p}, set(known), (n, rnd))
                self.assertTrue(all(a != b for a, b in got), (n, rnd))
                keys = [frozenset(p) for p in got]
                self.assertEqual(len(set(keys)), len(keys), f"n={n} round {rnd}: no pair twice")
                if rnd < 1 + (n == 20):  # the distinct offsets: n=20 has two, n=24 one
                    self.assertFalse(rivals & set(keys), f"n={n} round {rnd}: new rivals")
                    rivals |= set(keys)

    def test_the_calibration_picker_records_twelve_answers(self):
        shown = self.fm_json("looks", "calibrate")
        pairs = shown["pairs"]
        self.assertEqual(len(pairs), 12)
        names = {os.path.splitext(n)[0] for n in os.listdir(LOOKS) if n.startswith("look-")}
        seen = [x for a, b in pairs for x in (a["look"], b["look"])]
        self.assertTrue(all(a["look"] != b["look"] for a, b in pairs))
        self.assertEqual(set(seen), names, "every look is shown")
        self.assertEqual(len({frozenset((a["look"], b["look"])) for a, b in pairs}), 12, "no pair twice")
        for few in ("a,b", "a," * 12 + "a", "a,b,x,a,b,a,b,a,b,a,b,a"):
            p = self.fm("looks", "calibrate", "--answers", few, check=False)
            self.assertNotEqual(p.returncode, 0, few)
        self.assertEqual(taste.load()["events"], [])
        answers = "a,b,n,a,b,a,b,a,b,a,b,a"
        got = self.fm_json("looks", "calibrate", "--round", str(shown["round"]), "--answers", answers)
        self.assertEqual(got["answered"], 12)
        cal = taste.load()["events"][-1]
        self.assertEqual(cal["signal"], "calibration")
        self.assertEqual(cal["answers"], answers.replace(",", ""))
        self.assertEqual([list(x) for x in cal["pairs"]], [[a["look"], b["look"]] for a, b in pairs])
        rank = {r["look"]: r["score"] for r in self.fm_json("looks")["looks"]}
        self.assertGreater(rank[pairs[0][0]["look"]], rank[pairs[0][1]["look"]], "a pick outranks its rival")
        again = self.fm_json("looks", "calibrate")
        self.assertNotEqual(again["round"], shown["round"])
        self.assertTrue({frozenset((a["look"], b["look"])) for a, b in again["pairs"]}.isdisjoint(
            {frozenset((a["look"], b["look"])) for a, b in pairs}), "the next calibration meets new rivals")

    def test_the_keyboard_picker_takes_one_key_a_pair_and_stops_on_q(self):
        pairs = taste.pairs(taste.looks())
        keys = iter("x1" + "2" * 10 + "n")  # x isn't an answer: asked again
        out = io.StringIO()
        self.assertEqual(taste.pick(pairs, lambda: next(keys), out.write), list("a" + "b" * 10 + "n"))
        self.assertIn("12/12", out.getvalue())
        # arrows, Shift+arrows, Home/End and F-keys send escape sequences, some with digits: none is an answer
        noise = "\x1b[A\x1b[1;2A\x1b[1~\x1b[15~\x1bOP\x1b[2;5B\x1ba"
        keys = iter(noise + "2" + noise + "1" * 11)
        self.assertEqual(taste.pick(pairs, lambda: next(keys), io.StringIO().write), list("b" + "a" * 11))
        keys = iter("\x1b[1;2")  # a sequence cut off by the end of input stops, it answers nothing
        self.assertIsNone(taste.pick(pairs, lambda: next(keys, ""), io.StringIO().write))
        keys = iter("12q")
        self.assertIsNone(taste.pick(pairs, lambda: next(keys), io.StringIO().write))
        self.assertIsNone(taste.pick(pairs, lambda: "", io.StringIO().write), "end of input stops it too")

    def test_ranking_orders_looks_by_what_was_kept(self):
        taste.record("kept", "look-observatory")
        taste.record("kept", "look-observatory")
        taste.record("kept", "look-lido")
        taste.record("not", "look-cartographer", why="too busy")
        order = [r["look"] for r in taste.rank()]
        self.assertEqual(order[:2], ["look-observatory", "look-lido"])
        self.assertEqual(order[-1], "look-cartographer")
        self.assertIn("look-observatory", self.fm("looks").stdout.splitlines()[1])


class Sources(ForemanTestCase):
    def _page(self):
        site = os.path.join(self.tmp, "site")
        os.makedirs(site)
        with open(os.path.join(site, "style.css"), "w") as f:
            f.write("body { color: #262D2E }\n")
        page = os.path.join(site, "page.html")
        with open(page, "w") as f:  # Lido's accent and ground, a linked sheet and a remote image it must not follow
            f.write('<link rel="stylesheet" href="style.css"><img src="https://example.com/x.png">'
                    '<p style="color:#076E87;background:#F0F9FA">Book a lane at the pool: timetable</p>')
        return site, page

    def test_nothing_reads_a_file_the_user_didnt_name(self):
        site, page = self._page()
        before = os.stat(page).st_mtime_ns
        with Opens() as seen:
            got = taste.learn([page])
            taste.rank()
            taste.record("kept", "look-lido")
            taste.pairs(taste.looks())
        self.assertEqual(got["learned"][0]["look"], "look-lido")
        self.assertEqual(seen.outside(LOOKS, c.state_dir()), [os.path.realpath(page)])
        self.assertNotIn(os.path.join(site, "style.css"), seen.paths)
        self.assertEqual(os.stat(page).st_mtime_ns, before, "the named file is only read")
        event = [e for e in taste.load()["events"] if e["signal"] == "learned"][0]
        self.assertEqual(event["source"], "page.html", "the ledger keeps the file's name, never its text or path")

    def test_only_local_regular_files_and_only_so_much_of_them(self):
        site, page = self._page()
        for bad in ("https://example.com/page.html", "file:///etc/passwd", site, os.path.join(site, "missing")):
            with self.subTest(bad=bad):
                got = taste.learn([bad])
                self.assertEqual(got["learned"], [])
                self.assertEqual(len(got["skipped"]), 1)
        big = os.path.join(self.tmp, "big.css")
        with open(big, "w") as f:
            f.write("a" * (taste.SOURCE_CAP + 10) + " #076E87 #F0F9FA")
        self.assertEqual(len(taste.read_named(big)), taste.SOURCE_CAP)
        self.assertEqual(taste.learn([big])["learned"], [], "nothing past the cap is read")
        fifo = os.path.join(self.tmp, "pipe")
        os.mkfifo(fifo)
        self.assertEqual(len(taste.learn([fifo])["skipped"]), 1, "a pipe is refused, not waited on")
        heic = os.path.join(self.tmp, "x.heic")
        with open(heic, "wb") as f:
            f.write(b"\0\0\0\x18ftypheic #076E87 #076E87")
        self.assertEqual(taste.learn([heic])["learned"], [], "an unknown binary is noise, not colours")
        with mock.patch.object(taste, "LEARN_MAX", 1):
            got = taste.learn([page, page])
        self.assertEqual((len(got["learned"]), len(got["skipped"])), (1, 1), "a run reads LEARN_MAX files at most")
        link = os.path.join(self.tmp, "link.html")
        os.symlink(page, link)
        got = taste.learn([link])
        self.assertEqual(got["learned"], [], "a symlink is refused: the user names the file itself")
        self.assertIn("symlink", got["skipped"][0]["why"])
        p = self.fm("looks", "learn", check=False)
        self.assertNotEqual(p.returncode, 0, "learn needs a path the user names")
        p = self.fm("looks", "learn", os.path.join(site, "missing"), check=False)
        self.assertEqual(p.returncode, 1, "every file skipped: nothing learned is a failure")
        self.assertIn("Skipped: missing", p.stdout)
        self.assertEqual(self.fm("looks", "learn", page).returncode, 0)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow isn't installed: photos are skipped, said so above")
    def test_a_photo_teaches_its_colours_and_a_bomb_is_never_decoded(self):
        lido = os.path.join(self.tmp, "pool.png")
        with open(lido, "wb") as f:
            f.write(png(8, 8, rgb=(0x07, 0x6E, 0x87)))
        self.assertEqual(taste.learn([lido])["learned"][0]["look"], "look-lido")
        bomb = os.path.join(self.tmp, "bomb.png")
        with open(bomb, "wb") as f:  # a small file that declares 10000x10000: the size is checked before decoding
            f.write(png(10000, 10000, rows=False))
        with mock.patch("PIL.Image.Image.convert", side_effect=AssertionError("decoded")):
            got = taste.learn([bomb])
        self.assertEqual(got["learned"], [])
        self.assertIn("megapixels", got["skipped"][0]["why"])

    def test_a_photo_needs_an_installed_reader_and_says_so(self):
        photo = os.path.join(self.tmp, "shot.png")
        with open(photo, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
        with mock.patch.dict("sys.modules", {"PIL": None, "PIL.Image": None}):
            got = taste.learn([photo])
        self.assertEqual(got["learned"], [])
        self.assertIn("Pillow", got["skipped"][0]["why"])
        self.assertNotIn("install", got["skipped"][0]["why"].split("Pillow")[0], "detected, never installed")


class CodeApp(ForemanTestCase):
    def test_fm_code_has_a_keyboard_picker_that_records_twelve_answers(self):
        from sampo import smithtui as tui
        app = tui.App(tui.load_config()[0], spawn=lambda *a, **k: None)
        app.key("L")
        self.assertEqual(app.mode, "looks")
        text = tui.render(app.state, 120, 40, app.cfg, app).text()
        self.assertIn("1/12", text)
        first = taste.looks()[taste.pairs(taste.looks())[0][0]]["title"]
        self.assertIn(first, text)
        for k in "1" * 11:
            app.key(k)
        app.key("\x1b")  # stopped before twelve: nothing recorded
        self.assertEqual(app.mode, "main")
        self.assertEqual(taste.load()["events"], [])
        app.key("L")
        screen = tui.render(app.state, 120, 40, app.cfg, app)
        self.assertNotEqual(screen.style_of(first), "select", "no row is highlighted until the user moves")
        app.key("\n")  # so Enter alone answers nothing
        self.assertEqual(app.look_answers, [])
        app.key("KEY_DOWN")
        app.key("KEY_DOWN")
        app.key("\n")  # Enter takes the highlighted one: the second look
        app.key("1")
        app.key("KEY_BACKSPACE")  # back one answer: pair 2 is asked again
        self.assertEqual(app.look_answers, ["b"])
        self.assertIn("2/12", tui.render(app.state, 120, 40, app.cfg, app).text())
        for k in "x" + "12n" * 3 + "1":  # x answers nothing
            app.key(k)
        self.assertEqual(app.mode, "looks")
        app.key("n")
        self.assertEqual(app.mode, "main")
        self.assertEqual(taste.load()["events"][-1]["answers"], "b" + "abn" * 3 + "an")
        self.assertIn("12 answers", app.message)
        with mock.patch.object(taste, "looks", return_value={}):  # no cards to pair: a note, the screen stays
            app.key("L")
        self.assertEqual(app.mode, "main")
        self.assertIn("no calibration", app.message)


class Divergence(ForemanTestCase):
    """T-1536: a tournament the user answers match by match (each winner +1, each loser -1), opposite day, a lookbook
    page, and five historical idioms of one layout for the directions skill."""

    def test_a_four_look_tournament_is_answered_match_by_match(self):
        first = json.loads(self.fm("looks", "tournament", "--json").stdout)
        seeds = first["seeds"]
        self.assertEqual((len(seeds), len(first["next"])), (4, 2))
        self.assertEqual([x["look"] for x in first["next"][0]], [seeds[0], seeds[3]])  # 1 v 4, then 2 v 3
        half = json.loads(self.fm("looks", "tournament", *seeds, "--answers", "a,b", "--json").stdout)
        self.assertEqual([x["look"] for x in half["next"][0]], [seeds[0], seeds[2]])  # the final: 1 v 3
        self.assertIn("--answers a,b,<a|b>", half["then"])
        done = json.loads(self.fm("looks", "tournament", *seeds, "--answers", "a,b,b", "--json").stdout)
        self.assertEqual(done["champion"], seeds[2])
        top = json.loads(self.fm("looks", "--json").stdout)["looks"][0]
        self.assertEqual((top["look"], top["score"], top.get("picked")), (seeds[2], 2, 2))
        for bad in (["--n", "3"], ["--answers", "n"], ["--answers", "a,b,a,a"]):
            p = self.fm("looks", "tournament", *(seeds if bad[0] == "--answers" else []), *bad, check=False)
            self.assertNotEqual(p.returncode, 0, bad)
        duel = json.loads(self.fm("looks", "tournament", "lido", "Night market", "--answers", "a", "--json").stdout)
        self.assertEqual(duel["champion"], "look-lido")

    def test_opposite_day_and_the_lookbook(self):
        got = json.loads(self.fm("looks", "opposite", "lido", "--json").stdout)["opposites"]
        self.assertEqual(len(got), len(taste.looks()) - 1)
        self.assertNotIn("look-lido", [x["look"] for x in got])
        self.assertEqual([x["distance"] for x in got], sorted((x["distance"] for x in got), reverse=True))
        page = os.path.join(self.tmp, "book.html")
        self.fm("looks", "book", "--n", "3", "--out", page)
        text = read_text(page)
        self.assertEqual(text.count("<section>"), 3)
        self.assertNotIn("<script", text)
        self.assertRegex(text, r'<li style="background:#[0-9a-f]{6};color:#(000|fff)">#[0-9a-f]{6}</li>')
        self.assertNotEqual(self.fm("looks", "book", check=False).returncode, 0, "it writes a page: --out")

    def test_the_skill_and_command_offer_them(self):
        skill = read_text(os.path.join(PLUGIN, "skills", "directions", "SKILL.md"))
        for word in ("sampo looks opposite", "sampo looks book", "/sampo:looks tournament", "references/idioms.md"):
            self.assertIn(word, skill)
        idioms = read_text(os.path.join(PLUGIN, "skills", "directions", "references", "idioms.md"))
        for era in ("Swiss", "Bauhaus", "Art Deco", "Memphis", "Brutalist"):
            self.assertIn(era, idioms)
        cmd = read_text(os.path.join(PLUGIN, "commands", "looks.md"))
        for word in ("sampo looks tournament --json", "no Neither", "sampo looks opposite", "sampo looks book"):
            self.assertIn(word, cmd)


class Surfaces(unittest.TestCase):
    def test_claude_code_has_a_user_only_command_and_the_directions_skill_ranks_by_it(self):
        cmd = read_text(os.path.join(PLUGIN, "commands", "looks.md"))
        self.assertIn("disable-model-invocation: true", cmd)
        for word in ("AskUserQuestion", "sampo looks calibrate", "--answers", "--round", "sampo looks learn"):
            self.assertIn(word, cmd)
        skill = read_text(os.path.join(PLUGIN, "skills", "directions", "SKILL.md"))
        for word in ("sampo looks", "sampo looks keep", "sampo looks not", "sampo looks revert", "the least negative",
                     "Record nothing in `sampo looks`: it holds the user's choices only.",
                     "run `sampo looks keep` or `sampo looks revert` yourself"):
            self.assertIn(word, skill)
        for word in ("--why '", "a quote", "AskUserQuestion is denied", "record nothing",
                     "run `sampo looks keep` or `sampo looks revert` yourself"):
            self.assertIn(word, cmd)


if __name__ == "__main__":
    unittest.main()
