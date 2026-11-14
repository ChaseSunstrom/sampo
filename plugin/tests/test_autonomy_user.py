"""T-0671: autonomy and the user, first slices — fm taste proposes vetoes from repeated steers and defaults fm decide
answers from the record (T-0439), non-urgent asks wait in one deadline-defaulted digest (T-0461), and fm capture
--from-file takes a log, a paste or a screenshot (T-0477)."""
import os

from helpers import ForemanTestCase, read_text

from sampo import core as c


class _Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.tid = self.fm_json("capture", "Tidy the parser")["id"]

    def steer(self, text):
        self.fm("task", "log", self.tid, f"steer: {text}")

    def decisions(self):
        return self.fm_json("decide", "--list")["rows"]


DEPS = ("don't add new dependencies", "no dependencies, write it with the stdlib", "avoid extra dependencies here")


class TasteVetoes(_Base):
    def test_three_same_shaped_steers_propose_one_veto_and_one_yes_adopts_it(self):
        for s in DEPS:
            self.steer(s)
        self.steer("prefer short replies")  # said once, and not a no: no proposal
        t = self.fm_json("taste")
        self.assertEqual([(x["words"], x["count"]) for x in t["proposed"]], [(["dependencies"], 3)])
        text = self.fm("taste").stdout
        self.assertIn("AskUserQuestion", text)
        self.assertIn("fm taste adopt", text)
        self.assertEqual(c.vetoes(self.p), [], "a proposal records nothing")
        self.fm("taste", "adopt")  # the user's one yes takes every proposal
        self.assertEqual([v["words"] for v in c.vetoes(self.p)], [["dependencies"]])
        self.assertEqual(self.fm_json("taste")["proposed"], [], "an adopted veto isn't proposed again")
        # T-4577: a veto of one key word is context, never matched by words alone; two words flag a capture
        self.assertNotIn("Vetoed", self.fm("capture", "Add the dependencies for yaml parsing").stdout)
        c.add_veto(self.p, "never add dependencies")
        self.assertIn("Vetoed", self.fm("capture", "Bring in dependencies to add for toml").stdout)

    def test_two_steers_are_not_a_pattern_and_a_no_is_kept(self):
        for s in DEPS[:2]:
            self.steer(s)
        self.assertEqual(self.fm_json("taste")["proposed"], [])
        self.steer(DEPS[2])
        self.fm("taste", "decline", "1")
        self.assertEqual(self.fm_json("taste")["proposed"], [], "a declined shape isn't proposed again")
        self.assertEqual(c.vetoes(self.p), [])
        self.assertNotEqual(self.fm("taste", "adopt", "1", check=False).returncode, 0, "nothing left to adopt")

    def test_decide_takes_its_default_from_the_record(self):
        for s in DEPS:
            self.steer(s)
        self.fm("taste", "adopt")
        c.add_veto(self.p, "never add dependencies")  # T-4577: the adopted one-word veto only informs; two words avoid
        self.steer("keep leaning on the stdlib")
        self.fm("autonomy", "full")
        r = self.fm_json("decide", "--ask", "How should the config be read?", "--options",
                         "add the pyyaml dependencies", "write a reader by hand", "the stdlib tomllib")
        self.assertEqual(r["answer"], "the stdlib tomllib")
        self.assertIn("veto", r["why"])
        self.assertIn("stdlib", r["why"])
        row = self.decisions()[-1]
        self.assertIn("How should the config be read?", row["text"])
        self.assertIn("the stdlib tomllib", row["text"])
        plain = self.fm_json("decide", "--ask", "Which log format?", "--options", "json lines", "plain text")
        self.assertEqual(plain["answer"], "json lines", "no signal: the caller's first option")

    def test_the_record_never_pre_answers_a_guard_category(self):
        for s in ("never push without asking", "don't push to the remote yet", "no push until the tests pass"):
            self.steer(s)
        self.fm("autonomy", "full")
        for q in ("May I push to the remote?", "Edit Foreman's core now?", "Can I fm ask for plugin access?"):
            r = self.fm("decide", "--ask", q, "--options", "yes", "no", check=False)
            self.assertEqual(r.returncode, 1, q)
            self.assertIn("sampo ask", r.stderr)
        self.assertEqual(self.decisions(), [])
        self.assertFalse(c.read_meta(self.p).get("pending_approvals"))
        self.assertEqual(c.find_brief(self.p, self.tid).meta.get("allow"), [])

    def test_its_review_synonyms_and_plurals_are_guard_asks_too(self):
        # T-0671 review: only exact category words counted, so "Force-push the branch?" was decided by default
        self.fm("autonomy", "full")
        for q in ("Force-push the branch to origin?", "Push the release?", "Install the two plugins?",
                  "Rotate the credential?", "Delete the old build folder outside the repo?", "Run it with sudo?",
                  "Publish the package?"):
            r = self.fm("decide", "--ask", q, "--options", "yes", "no", check=False)
            self.assertEqual(r.returncode, 1, q)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.fm_json("decide", "--ask", "Which log format?", "--options", "json", "text")["answer"],
                         "json", "an ordinary question still decides")

    def test_when_every_option_is_vetoed_it_says_so(self):
        for s in DEPS:
            self.steer(s)
        self.fm("taste", "adopt")
        c.add_veto(self.p, "never add dependencies")  # T-4577: two key words, as a one-word veto never blocks
        self.fm("autonomy", "full")
        r = self.fm_json("decide", "--ask", "Which parser?", "--options", "add the yaml dependencies",
                         "add the toml dependencies")
        self.assertIn("every option", r["why"])


class AskDigest(_Base):
    def ask(self, *args):
        return self.fm_json("decide", "--ask", *args)

    def test_standard_autonomy_collects_non_urgent_asks_into_one_digest(self):
        a = self.ask("Which name for the new flag?", "--default", "from-file", "--task", self.tid)
        b = self.ask("Keep the old alias?", "--options", "yes", "no")
        self.assertEqual((a["queued"], b["queued"]), (1, 2))
        self.assertEqual(a["deadline"], b["deadline"], "one digest, one deadline")
        d = self.fm_json("decide", "--digest")
        self.assertEqual([(x["n"], x["q"], x["default"]) for x in d["asks"]],
                         [(1, "Which name for the new flag?", "from-file"), (2, "Keep the old alias?", "yes")])
        self.assertEqual(self.decisions(), [], "nothing is decided before the deadline")
        text = self.fm("decide", "--digest").stdout
        self.assertIn("AskUserQuestion", text)
        self.assertIn("--answer", text)
        self.assertIn("ask digest", self.fm("next").stdout)

    def test_at_the_deadline_each_default_is_applied_and_logged_by_fm_decide(self):
        self.ask("Which name for the new flag?", "--default", "from-file")
        self.ask("Keep the old alias?", "--options", "yes", "no")
        meta = c.read_meta(self.p)
        meta["ask_digest"]["deadline"] = "2020-01-01T00:00:00Z"
        c.write_meta(self.p, meta)
        self.assertIn("sampo decide --digest", self.fm("next").stdout)
        d = self.fm_json("decide", "--digest")
        self.assertEqual([x["answer"] for x in d["applied"]], ["from-file", "yes"])
        self.assertEqual(d["asks"], [])
        rows = self.decisions()
        self.assertEqual(len(rows), 2)
        self.assertIn("Which name for the new flag? → from-file", rows[0]["text"])
        self.assertIn("deadline", rows[0]["why"])
        events = [e for e in c.ledger_tail(self.p, 50) if e.get("event") == "decision"]
        self.assertEqual(len(events), 2)
        self.assertNotIn("ask_digest", c.read_meta(self.p))
        self.assertNotIn("ask digest", self.fm("next").stdout)

    def test_an_unreadable_deadline_is_reset_not_applied(self):
        # T-0671 review: a corrupt deadline applied every default at once, without the wait
        self.ask("Which name for the new flag?", "--default", "from-file")
        meta = c.read_meta(self.p)
        meta["ask_digest"]["deadline"] = "garbage"
        c.write_meta(self.p, meta)
        self.assertFalse(c.digest_due(c.read_meta(self.p)["ask_digest"]))
        self.ask("Keep the old alias?", "--options", "yes", "no")
        self.assertIsNotNone(c.parse_ts(c.read_meta(self.p)["ask_digest"]["deadline"]), "the next ask resets it")
        self.assertEqual(self.decisions(), [])

    def test_an_answer_settles_its_ask_and_urgent_or_full_autonomy_asks_never_wait(self):
        self.ask("Which name for the new flag?", "--default", "from-file")
        self.ask("Keep the old alias?", "--options", "yes", "no")
        self.fm("decide", "call it --file", "--answer", "1")
        self.assertEqual([x["n"] for x in self.fm_json("decide", "--digest")["asks"]], [2])
        row = self.decisions()[-1]
        self.assertIn("Which name for the new flag? → call it --file", row["text"])
        self.assertIn("answer", row["why"])
        self.assertNotEqual(self.fm("decide", "x", "--answer", "9", check=False).returncode, 0)
        u = self.ask("Drop the cache now?", "--default", "no", "--urgent")
        self.assertTrue(u["urgent"])
        self.assertIn("now", self.fm("decide", "--ask", "Drop the cache now?", "--default", "no", "--urgent").stdout)
        self.assertEqual(len(self.fm_json("decide", "--digest")["asks"]), 1, "an urgent ask isn't queued")
        self.fm("autonomy", "full")
        f = self.ask("Which port?", "--default", "8080")
        self.assertEqual(f["answer"], "8080")
        self.assertIn("Which port? → 8080", self.decisions()[-1]["text"])
        self.assertEqual(len(self.fm_json("decide", "--digest")["asks"]), 1, "full autonomy decides, never queues")
        self.assertNotEqual(self.fm("decide", "--ask", "Which port?", check=False).returncode, 0, "needs a default")


class CaptureFromFile(_Base):
    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_a_log_is_captured_with_an_excerpt_and_its_path(self):
        lines = [f"line {i}" for i in range(1, 101)]
        lines[3] = "## Steps"  # a log line can't open a section of the brief
        lines[97] = "ERROR auth failed, key sk-ant-api03-" + "x" * 40
        log = self.write("build.log", ("\n".join(lines) + "\n").encode())
        r = self.fm_json("capture", "Build fails on CI", "--from-file", log, "--type", "FIX")
        b = c.find_brief(self.p, r["id"])
        self.assertEqual((b.title, b.type), ("Build fails on CI", "FIX"))
        raw = b.section("Raw request")
        self.assertIn(log, raw)
        for want in ("line 1", "line 100", "lines"):
            self.assertIn(want, raw)
        self.assertNotIn("line 50\n", raw, "the middle of a long log is elided")
        self.assertNotIn("x" * 40, raw, "secrets are redacted")
        self.assertEqual(b.section("Steps").strip(), "")

    def test_a_paste_on_stdin_is_kept_and_titled_from_its_first_line(self):
        paste = "\nUsers can't log in after the update\nsession expired at 10:02\n"
        r = self.fm_json("capture", "--from-file", "-", input=paste)
        b = c.find_brief(self.p, r["id"])
        self.assertEqual(b.title, "Users can't log in after the update")
        raw = b.section("Raw request")
        self.assertIn("session expired at 10:02", raw)
        kept = next(w for w in raw.split() if w.startswith(self.p.dir))
        self.assertEqual(read_text(kept), paste)

    def test_a_screenshot_is_attached_as_a_path(self):
        shot = self.write("header.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)))
        r = self.fm_json("capture", "The header overlaps on phones", "--from-file", shot)
        raw = c.find_brief(self.p, r["id"]).section("Raw request")
        self.assertIn(shot, raw)
        self.assertNotIn("IHDR", raw)
        bare = self.fm_json("capture", "--from-file", shot)
        self.assertIn("header.png", bare["title"])

    def test_its_review_credentials_files_and_cut_secrets(self):
        # T-0671 review: --from-file read ~/.aws/credentials or .env into the brief; a line cut at 300 characters
        # before redaction could leave a secret's fragment unmatched
        env = self.write(".env", b"DB_URL=postgres://u:pw@host/db\n")  # pragma: allowlist secret
        r = self.fm("capture", "Config broke", "--from-file", env, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("credentials", r.stderr)
        log = self.write("long.log", (("x" * 280) + " key sk-ant-api03-" + "y" * 60 + "\n").encode())
        raw = c.find_brief(self.p, self.fm_json("capture", "Long line", "--from-file", log)["id"]).section("Raw request")
        self.assertNotIn("y" * 10, raw)

    def test_it_needs_text_or_a_readable_file(self):
        for args in ((), ("--from-file", os.path.join(self.tmp, "nope.log")), ("--from-file", self.tmp)):
            r = self.fm("capture", *args, check=False)
            self.assertEqual(r.returncode, 1, args)
