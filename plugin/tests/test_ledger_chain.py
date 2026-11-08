"""SECURITY T-1065: each ledger line carries the hash of the one before (prev); fm verify-ledger proves no chained line
was edited or removed; fm task finish's receipt names the chain head; the chain survives fm tidy's monthly roll."""
import hashlib
import json
import os
import re
import threading
from unittest import mock

from helpers import ForemanTestCase, read_text

from sampo import core as c
from sampo import tidy


def h(raw):
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class _Chain(ForemanTestCase):
    """setUp, lines(), put() and verify(); no tests of its own (T-3815), so Cap shares them without rerunning Chain's."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.ledger = os.path.join(self.p.dir, "ledger.jsonl")

    def lines(self):
        return read_text(self.ledger).splitlines()

    def put(self, lines):
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in lines))

    def verify(self, *args):
        return self.fm("verify-ledger", *args, check=False)

class Chain(_Chain):
    def test_ledger_events_reads_every_month(self):
        # a report over a window reads the months fm tidy rolled into archive/, not just the live file's tail
        os.makedirs(os.path.join(self.p.dir, "archive"), exist_ok=True)
        with open(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl"), "w") as f:
            f.write(json.dumps({"ts": "2026-01-05T00:00:00Z", "event": "correction", "task": "T-0001"}) + "\n")
            f.write(json.dumps({"ts": "2026-01-06T00:00:00Z", "event": "focus", "task": "T-0001"}) + "\n")
            f.write("not json\n")
        c.log_event(self.p, "correction", task="T-0002")
        got = [e["task"] for e in c.ledger_events(self.p, ("correction",))]
        self.assertEqual(got, ["T-0001", "T-0002"])
        self.assertEqual([e["task"] for e in c.ledger_events(self.p, ("correction",), since="2026-02-01")], ["T-0002"])
        self.assertGreaterEqual(len(c.ledger_events(self.p)), 3)

    def test_ledger_events_since_reads_only_the_tail(self):
        # T-2015: the risk budget asked for today's events and parsed all 18 MB of the ledger on every hook
        old = json.dumps({"ts": "2026-01-05T00:00:00Z", "event": "note", "data": {"pad": "x" * 200}})
        self.put([old] * 3000 + [json.dumps({"ts": f"2026-03-0{i + 1}T00:00:00Z", "event": "note", "data": {"i": i}})
                                 for i in range(3)])
        real, calls = json.loads, []
        with mock.patch.object(c.json, "loads", side_effect=lambda s, *a, **k: calls.append(1) or real(s, *a, **k)):
            got = c.ledger_events(self.p, since="2026-03-01")
        self.assertEqual([e["data"]["i"] for e in got], [0, 1, 2])
        self.assertLess(len(calls), 500, "older lines past the tail are never parsed")

    def test_last_change_parses_only_its_tasks_touched_lines(self):
        # T-2017: every hook parsed 5,000 ledger lines to find one task's file edits
        ev = lambda ts, event, task: json.dumps({"ts": ts, "event": event, "task": task, "data": {"note": "T-0001"}})
        self.put([ev("2026-03-01T00:00:00Z", "note", "T-0002")] * 3000 + [
            ev("2026-03-02T00:00:00Z", "touched", "T-0001"), ev("2026-03-03T00:00:00Z", "touched", "T-0002"),
            ev("2026-03-04T00:00:00Z", "focus", "T-0001"), ev("2026-03-05T00:00:00Z", "touched", "T-00011")])
        real, calls = json.loads, []
        with mock.patch.object(c.json, "loads", side_effect=lambda s, *a, **k: calls.append(1) or real(s, *a, **k)):
            self.assertEqual(c.last_change(self.p, "T-0001"), "2026-03-02T00:00:00Z")
        self.assertLess(len(calls), 10, "lines without both words are never parsed")
        self.assertEqual(c.last_change(self.p, "T-0009"), "")

    def test_tail_rows_with_keep_parses_only_matching_lines_of_the_last_n(self):
        # T-2025: decoding and splitting every line of a 4.7 MB tail cost 12 ms to find 23 prompt lines
        ev = lambda i, event: json.dumps({"i": i, "event": event, "data": {"note": "é"}})
        self.put([ev(0, "prompt"), ev(1, "note"), ev(2, "prompt"), ev(3, "note"), ev(4, "prompt"),
                  '{"event": "prompt", "cut', ev(6, "note"), ev(7, "prompt"), ev(8, "prompt-ish"), ev(9, "prompt")])
        real, calls = json.loads, []
        with mock.patch.object(c.json, "loads", side_effect=lambda s, *a, **k: calls.append(1) or real(s, *a, **k)):
            rows = c._tail_rows(self.ledger, 5, keep=('"prompt"',))
        self.assertEqual([e["i"] for e in rows], [7, 9], "the last 5 lines only; the damaged line is skipped")
        self.assertEqual(len(calls), 3, "only lines holding the word are parsed")
        self.assertEqual([e["i"] for e in c._tail_rows(self.ledger, 99, keep=('"prompt"', '"i": 2'))], [2],
                         "every word must be on the line")

    def test_last_change_reads_the_ledger_once_until_it_grows(self):
        # T-2024: the prompt hook's state and its Next line each read the ledger's tail for the same answer
        ev = lambda ts: json.dumps({"ts": ts, "event": "touched", "task": "T-0001"})
        self.put([ev("2026-03-02T00:00:00Z")])
        with mock.patch.object(c, "_tail_rows", wraps=c._tail_rows) as tail:
            self.assertEqual(c.last_change(self.p, "T-0001"), "2026-03-02T00:00:00Z")
            self.assertEqual(c.last_change(self.p, "T-0001"), "2026-03-02T00:00:00Z")
            self.assertEqual(tail.call_count, 1)
            with open(self.ledger, "a", encoding="utf-8") as f:
                f.write(ev("2026-03-03T00:00:00Z") + "\n")
            self.assertEqual(c.last_change(self.p, "T-0001"), "2026-03-03T00:00:00Z", "an append is read")

    def test_untouched_ledger_verifies(self):
        for i in range(5):
            c.log_event(self.p, "note", data={"i": i})
        lines = self.lines()
        self.assertTrue(all(json.loads(x).get("prev") for x in lines), "every line fm writes is chained")
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(h(lines[-1]), r.stdout, "OK names the head: the hash of the last line")
        self.assertIn("only with --head", r.stdout, "OK says the last line's edit needs the receipt's head")

    def test_broken_chain_has_no_head(self):
        for i in range(3):
            c.log_event(self.p, "note", data={"i": i})
        lines = self.lines()
        lines[1] = lines[1].replace('"event": "', '"event": "x', 1)
        self.put(lines)
        r = self.verify("--json")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIsNone(json.loads(r.stdout)["head"], "a broken chain has no head, not the genesis hash")

    def archive_unreadable(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        os.makedirs(os.path.join(self.p.dir, "archive"), exist_ok=True)
        path = os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"ts": "2026-01-01T00:00:00Z", "event": "note"}) + "\n")
        os.chmod(path, 0)  # the test's folder is removed whole: no mode to restore

    def test_unreadable_archive_never_loses_an_event(self):
        self.archive_unreadable()
        self.put([])  # an empty live ledger: the first line's prev would come from the archive
        c.log_event(self.p, "note", data={"kept": 1})
        self.assertEqual([json.loads(x)["data"] for x in self.lines()], [{"kept": 1}])

    def test_no_lock_never_loses_an_event(self):
        n = len(self.lines())
        with mock.patch.object(c.fcntl, "flock", side_effect=OSError(37, "No locks available")):
            c.log_event(self.p, "note", data={"kept": 1})
        self.assertEqual(len(self.lines()), n + 1)
        self.assertEqual(json.loads(self.lines()[-1])["data"], {"kept": 1})
        c.log_event(self.p, "note")
        self.assertEqual(self.verify().returncode, 0, "the unchained line is a gap, not a break")

    def test_edit_is_named(self):
        for i in range(5):
            c.log_event(self.p, "note", data={"i": i})
        lines = self.lines()
        lines[2] = lines[2].replace('"event": "', '"event": "x', 1)
        self.put(lines)
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ledger.jsonl:3", r.stdout + r.stderr)

    def test_deletion_is_named(self):
        for i in range(5):
            c.log_event(self.p, "note", data={"i": i})
        lines = self.lines()
        n = len(lines)
        del lines[n - 3]  # line n-2 goes: line n-3 is the one its successor no longer follows
        self.put(lines)
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"ledger.jsonl:{n - 3}", r.stdout + r.stderr)

    def test_first_line_deleted_is_caught(self):
        c.log_event(self.p, "note")
        self.put(self.lines()[1:])
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ledger.jsonl:1", r.stdout + r.stderr)

    def test_truncation_mid_line_is_named(self):
        for i in range(3):
            c.log_event(self.p, "note", data={"i": i})
        n = len(self.lines())
        with open(self.ledger, "rb+") as f:
            f.truncate(os.path.getsize(self.ledger) - 10)
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"ledger.jsonl:{n}", r.stdout + r.stderr)
        c.log_event(self.p, "note")  # a write after the cut starts its own line; the cut one stays named
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"ledger.jsonl:{n}", r.stdout + r.stderr)

    def crash(self, keep=0):
        """What an unclean shutdown leaves (ext4, 2026-10-22): the length kept, the unflushed last line zeroed."""
        with open(self.ledger, "rb") as f:
            body = f.read()
        start = body.rstrip(b"\n").rfind(b"\n") + 1 + keep
        with open(self.ledger, "wb") as f:
            f.write(body[:start] + b"\0" * (len(body) - start))
        return len(self.lines())

    def test_crash_damage_is_named_until_accepted_once(self):
        # T-1456: a zeroed line read as "not a whole JSON line", so every receipt said broken for good
        for i in range(3):
            c.log_event(self.p, "note", data={"i": i})
        n = self.crash()
        c.log_event(self.p, "note")  # the next write starts its own line, chained to the zeroed one
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"ledger.jsonl:{n}", r.stdout)
        self.assertIn("zero bytes", r.stdout)
        self.assertIn("--accept-damage", r.stdout)
        r = self.verify("--accept-damage")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("accepted", r.stdout)
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"ledger.jsonl:{n}", r.stdout, "the accepted damage stays named")
        from sampo import ledger
        self.assertIn("Ledger: head", ledger.receipt(self.p))
        c.log_event(self.p, "note", data={"after": 1})
        m = self.crash(keep=12)  # a second crash, this time mid-line: broken again until it too is accepted
        c.log_event(self.p, "note")
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"ledger.jsonl:{m}", r.stdout)
        self.assertEqual(self.verify("--accept-damage").returncode, 0)
        self.assertEqual(self.verify().returncode, 0)

    def cut(self, keep=40):
        """T-2664: a crash that kept a line's first bytes and lost the rest (volt-89d78b, 2026-10-23), no zeros."""
        with open(self.ledger, "rb") as f:
            body = f.read()
        start = body.rstrip(b"\n").rfind(b"\n") + 1
        with open(self.ledger, "wb") as f:
            f.write(body[:start + keep] + b"\n")
        return len(self.lines())

    def test_a_line_cut_short_mid_file_is_crash_damage_once_a_later_line_pins_it(self):
        for i in range(3):
            c.log_event(self.p, "note", data={"i": i})
        n = self.cut()
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)  # the last line: nothing pins its bytes yet, so it stays broken
        self.assertEqual(self.verify("--accept-damage").returncode, 1)
        c.log_event(self.p, "note")  # the next write chains to the cut bytes: they were cut at write, not edited
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"ledger.jsonl:{n}", r.stdout)
        self.assertIn("cut short", r.stdout)
        self.assertIn("--accept-damage", r.stdout)
        r = self.verify("--accept-damage")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"ledger.jsonl:{n}", r.stdout, "the accepted damage stays named")

    def test_an_ack_counts_only_for_the_damage_before_it(self):
        # the same number of zero bytes hashes the same: an ack names the line before it too, so it can't cover a
        # later crash of the same size
        c.log_event(self.p, "note", data={"i": 1})
        size = len(self.lines()[-1])
        self.crash()
        c.log_event(self.p, "note")
        self.assertEqual(self.verify("--accept-damage").returncode, 0)
        c.log_event(self.p, "note", data={"i": 2})
        self.assertEqual(len(self.lines()[-1]), size)
        self.crash()
        c.log_event(self.p, "note")
        self.assertEqual(self.verify().returncode, 1)

    def test_lines_removed_before_crash_damage_are_caught(self):
        # T-1456 review: a zeroed line holds no prev, so lines before it could go unseen; the first write after the
        # crash pins the line before the damage
        for i in range(5):
            c.log_event(self.p, "note", data={"i": i})
        self.crash()
        c.log_event(self.p, "note")
        lines = self.lines()
        self.put(lines[:2] + lines[-2:])  # lines before the zeroed one removed
        r = self.verify("--accept-damage")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Nothing accepted", r.stdout)
        self.assertNotIn("ledger_damage_ack", read_text(self.ledger))
        self.put(lines)
        self.assertEqual(self.verify("--accept-damage").returncode, 0, "untouched, the same damage is accepted")

    def test_accept_damage_checks_the_head_first(self):
        c.log_event(self.p, "note", data={"i": 1})
        head = h(self.lines()[-1])
        self.crash()  # the anchored line itself is gone
        c.log_event(self.p, "note")
        r = self.verify("--accept-damage", "--head", head)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("ledger_damage_ack", read_text(self.ledger))

    def test_zeroed_lines_before_the_chain_predate_it(self):
        self.put(["\0" * 40, json.dumps({"ts": "2026-09-01T00:00:00Z", "event": "note"})])
        c.log_event(self.p, "note")
        self.assertEqual(self.verify().returncode, 0, self.verify().stdout)

    def test_the_receipt_names_accepted_damage(self):
        from sampo import ledger
        c.log_event(self.p, "note")
        self.crash()
        c.log_event(self.p, "note")
        self.verify("--accept-damage")
        self.assertIn("1 crash-damaged line accepted", ledger.receipt(self.p))

    def test_accept_damage_never_accepts_an_edit(self):
        for i in range(3):
            c.log_event(self.p, "note", data={"i": i})
        lines = self.lines()
        self.put([x.replace('"i": 1', '"i": 9') for x in lines])
        self.assertNotEqual(self.lines(), lines)
        r = self.verify("--accept-damage")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Nothing accepted", r.stdout)
        self.assertNotIn("ledger_damage_ack", read_text(self.ledger))

    def test_head_anchor_catches_removed_tail(self):
        c.log_event(self.p, "note")
        head = h(self.lines()[-1])
        for i in range(3):
            c.log_event(self.p, "note", data={"i": i})
        self.assertEqual(self.verify("--head", head).returncode, 0)
        self.put(self.lines()[:-4])  # the anchored line and all after it: the rest still chains
        self.assertEqual(self.verify().returncode, 0, "tail truncation alone is invisible to a chain")
        r = self.verify("--head", head)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(head, r.stdout + r.stderr)

    def test_legacy_prefix_is_tolerated(self):
        legacy = [json.dumps({"ts": "2026-09-01T00:00:00Z", "event": "note", "data": {"i": i}}) for i in range(3)]
        self.put(legacy)
        c.log_event(self.p, "note")
        c.log_event(self.p, "note")
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ledger.jsonl:4", r.stdout)
        lines = self.lines()
        lines[2] = lines[2].replace('"i": 2', '"i": 9')  # the last legacy line: the first chained line vouches for it
        self.put(lines)
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ledger.jsonl:3", r.stdout + r.stderr)

    def test_unchained_line_after_the_start_is_a_reported_gap(self):
        c.log_event(self.p, "note")
        with open(self.ledger, "a") as f:  # an older Foreman still running in another session
            f.write(json.dumps({"ts": c.now(), "event": "note"}) + "\n")
        n = len(self.lines())
        c.log_event(self.p, "note")
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"ledger.jsonl:{n}", r.stdout)

    def test_concurrent_writers_keep_the_chain(self):
        def burst(k):
            for i in range(25):
                c.log_event(self.p, "note", data={"k": k, "i": i})
        threads = [threading.Thread(target=burst, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        r = self.verify("--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["chained"], len(self.lines()))

    def test_chain_survives_the_monthly_roll(self):
        self.put([])
        with mock.patch.object(c, "now", return_value="2026-01-15T00:00:00Z"):
            for i in range(5):
                c.log_event(self.p, "note", data={"pad": "y" * 50})
        with mock.patch.object(tidy, "LEDGER_ROLL_BYTES", 0):
            tidy._roll_ledger(self.p, True, [])  # every line is a past month's: the live file starts empty
        self.assertEqual(len(read_text(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl")).splitlines()), 5)
        c.log_event(self.p, "note")
        r = self.verify("--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["chained"], 7)  # 5 archived, ledger_rolled, note
        archived = read_text(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl")).splitlines()
        archived[1] = archived[1].replace("yyy", "yyz", 1)
        with open(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl"), "w") as f:
            f.write("".join(x + "\n" for x in archived))
        r = self.verify()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ledger-2026-01.jsonl:2", r.stdout + r.stderr)

    # T-1099: the roll reads bytes, keeps time order and takes the ledger's own flock
    def roll(self):
        with mock.patch.object(tidy, "LEDGER_ROLL_BYTES", 0), mock.patch.object(c, "now", return_value=MARCH):
            tidy._roll_ledger(self.p, True, [])

    def archived(self):
        with open(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl"), "rb") as f:
            return f.read().splitlines()

    def test_roll_survives_a_bad_byte(self):
        bad, deep = b'{"ts": "2026-01-15T00:00:00Z", "event": "note", "data": "\xff"}', b"[" * 100000  # RecursionError
        with open(self.ledger, "wb") as f:
            f.write(bad + b"\n" + deep + b"\n" + json.dumps({"ts": MARCH, "event": "note"}).encode() + b"\n")
        self.roll()
        self.assertEqual(self.archived(), [bad, deep], "the bad byte is archived byte for byte")
        self.assertEqual(json.loads(self.lines()[0])["ts"], MARCH)

    # T-1099 review: a roll that dies at any point loses no line, archives none twice and leaves a green chain
    def crash_roll(self, target, name, again=()):
        self.put([])
        for ts in ("2026-01-15T00:00:00Z", "2026-01-16T00:00:00Z", MARCH):
            with mock.patch.object(c, "now", return_value=ts):
                c.log_event(self.p, "note")
        with mock.patch.object(target, name, side_effect=OSError(5, "crash")), self.assertRaises(OSError):
            self.roll()
        c.log_event(self.p, "note", data={"after": 1})  # lands before the next roll
        self.roll()
        self.assertEqual([json.loads(x)["ts"] for x in self.archived()], ["2026-01-15T00:00:00Z", "2026-01-16T00:00:00Z"])
        self.assertEqual([json.loads(x)["data"] for x in self.lines()], [{}, {"after": 1}, *again],
                         "ledger_rolled only when lines left the live file")
        self.assertFalse(os.path.exists(self.ledger + ".rolling"))
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_crash_before_the_rewrite_archives_nothing_twice(self):
        self.crash_roll(c, "write_atomic", [{"2026-01": 2}])  # the live file still had them: rolled again, appended once

    def test_crash_mid_rewrite_is_restored_from_the_sidecar(self):
        self.crash_roll(os, "ftruncate")

    def test_crash_after_the_rewrite_drops_the_stale_sidecar(self):
        self.crash_roll(os, "remove")

    def test_unparseable_ts_rolls_with_its_neighbours(self):
        self.put(["not json"])  # a leading line with no ts goes with the first line that has one
        old = ["2026-01-15T00:00:00Z", "not a time", "2099-01-01T00:00:00Z", "2026-01-16T00:00:00Z"]  # 2099: a clock ahead
        for ts in old + [MARCH]:
            with mock.patch.object(c, "now", return_value=ts):
                c.log_event(self.p, "note")
        self.roll()
        self.assertEqual(self.archived()[0], b"not json")
        self.assertEqual([json.loads(x)["ts"] for x in self.archived()[1:]], old)
        self.assertEqual(json.loads(self.lines()[0])["ts"], MARCH)
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_roll_waits_for_a_writer_and_keeps_its_line(self):
        self.put([])
        with mock.patch.object(c, "now", return_value="2026-01-15T00:00:00Z"):
            for _ in range(3):
                c.log_event(self.p, "note")
        with open(self.ledger, "a+b") as f:  # a writer mid-log_event: holds the ledger's flock
            c.fcntl.flock(f, c.fcntl.LOCK_EX)
            t = threading.Thread(target=self.roll)
            t.start()
            t.join(0.3)
            self.assertTrue(t.is_alive(), "the roll waits for the ledger's flock")
            prev = c.ledger_hash(c._last_line(f))
            f.write((json.dumps({"prev": prev, "ts": MARCH, "event": "note", "data": {"racer": 1}}) + "\n").encode())
        t.join(5)
        self.assertEqual(len(self.archived()), 3)
        self.assertEqual(json.loads(self.lines()[0])["data"], {"racer": 1}, "the writer's line survives the roll")
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Cap(_Chain):
    """T-3403: log_event caps list and string fields before hashing."""
    def test_caps_lists_and_strings_and_chain_holds(self):
        c.log_event(self.p, "x", data={"files": list(range(500)), "short": [1], "s": "a" * 50000,
                                       "failed": list(range(300)), "failed_n": 999})
        d = json.loads(self.lines()[-1])["data"]
        self.assertEqual((len(d["files"]), d["files_n"]), (c.LEDGER_LIST_CAP, 500))
        self.assertNotIn("short_n", d)
        self.assertEqual((len(d["failed"]), d["failed_n"]), (c.LEDGER_LIST_CAP, 999))
        self.assertLess(len(d["s"]), 50000)
        c.log_event(self.p, "y")
        self.assertEqual(self.verify().returncode, 0)
        self.assertEqual(len(c.cap_payload({"s": "a" * 9999})["s"]), c.LEDGER_STR_CAP)
        self.assertEqual(c.cap_payload(None), None)

    def test_accept_damage_acks_every_key_in_chunks(self):
        from sampo import ledger
        dmg = [{"key": f"k{i}", "at": f"ledger.jsonl:{i}", "what": "zeroed"} for i in range(250)]
        with mock.patch.object(ledger, "chain", return_value={"damage_only": True, "damaged": dmg}):
            self.assertEqual(len(ledger.accept_damage(self.p)), 250)
        acks = [json.loads(x)["data"] for x in self.lines() if '"ledger_damage_ack"' in x]
        self.assertEqual(sorted(k for a in acks for k in a["keys"]), sorted(d["key"] for d in dmg))
        self.assertTrue(all(len(a["keys"]) <= c.LEDGER_LIST_CAP and "keys_n" not in a for a in acks))


MARCH = "2026-03-02T00:00:00Z"


class Receipt(ForemanTestCase):
    def test_finish_names_the_head(self):
        self.fm("task", "new", "First", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        r = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true")
        m = re.search(r"Ledger: head ([0-9a-f]{16}) \((\d+) chained lines?\)", r.stdout)
        self.assertTrue(m, r.stdout)
        self.assertEqual(self.fm("verify-ledger", "--head", m.group(1)).returncode, 0)

    def test_finish_json_carries_the_head(self):
        self.fm("task", "new", "First", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        out = json.loads(self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--json").stdout)
        self.assertRegex(out["ledger"]["head"], r"^[0-9a-f]{16}$")

    def unreadable_archive(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        self.fm("task", "new", "First", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        folder = os.path.join(c.find_project(self.repo).dir, "archive")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "ledger-2026-01.jsonl")
        with open(path, "w") as f:
            f.write("{}\n")
        os.chmod(path, 0)

    def test_unreadable_ledger_never_fails_the_finish(self):
        self.unreadable_archive()
        r = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Ledger: unreadable", r.stdout)
        self.assertTrue(r.stdout.strip().splitlines()[-1].startswith("Landed:"), r.stdout)

    def test_unreadable_ledger_never_fails_done_json(self):
        self.unreadable_archive()
        r = self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        led = json.loads(r.stdout)["ledger"]
        self.assertIsNone(led["head"])
        self.assertIn("unreadable", led)
