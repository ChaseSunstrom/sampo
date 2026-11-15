"""T-4704: sampo cost says where a session's context comes from: per ISO week the replies, the cache-read tokens per
reply, the replies per closed task, and the share of the context entering by source. Synthetic transcripts only: a
temp folder of made-up lines; the section prints numbers and tool names, never a transcript's text."""
import datetime
import json
import os

from helpers import ForemanTestCase

from sampo import core as c
from sampo import cost


def stamp(days_ago=0, hour=10):
    d = datetime.datetime.now(datetime.timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)
    return (d - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def week(ts):
    y, w, _ = datetime.date.fromisoformat(ts[:10]).isocalendar()
    return f"{y}-W{w:02d}"


def reply(mid, ts, blocks, cache=1000, new=10, out=5):
    return {"type": "assistant", "timestamp": ts, "sessionId": "s1", "message": {
        "id": mid, "usage": {"input_tokens": new, "cache_read_input_tokens": cache, "output_tokens": out},
        "content": blocks}}


def use(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def result(i, n):
    return {"type": "tool_result", "tool_use_id": i, "content": "x" * n}


def user(ts, *blocks):
    return {"type": "user", "timestamp": ts, "message": {"content": list(blocks)}}


class ContextSources(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.tmp, "transcripts")
        os.makedirs(self.folder)

    def write(self, lines, name="s1.jsonl", raw=""):
        with open(os.path.join(self.folder, name), "a") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines) + raw)

    def scan(self, days=30):
        ctx = {}
        msgs, _ = cost.scan(self.folder, cost._since(days), ctx=ctx)
        return msgs, ctx

    def test_a_reply_split_over_several_lines_counts_once(self):
        ts = stamp()
        self.write([reply("m1", ts, [use("a", "Read", file_path="f")], cache=500),
                    reply("m1", ts, [use("b", "Bash", command="ls")], cache=500),
                    reply("m2", ts, [], cache=300)])
        msgs, ctx = self.scan()
        got = cost.context_weeks(msgs, ctx, {})[week(ts)]
        self.assertEqual(got["replies"], 2)
        self.assertEqual(got["cache_read_per_reply"], 400)

    def test_a_bash_result_and_a_read_result_land_in_their_own_rows(self):
        ts = stamp()
        self.write([reply("m1", ts, [use("a", "Read", file_path="f"), use("b", "Bash", command="ls")]),
                    user(ts, result("a", 1000), result("b", 3000), result("zz", 200))])
        msgs, ctx = self.scan()
        src = cost.context_weeks(msgs, ctx, {})[week(ts)]["sources"]
        self.assertGreaterEqual(src["Read"], 1000)
        self.assertGreaterEqual(src["Bash"], 3000)
        self.assertLess(src["Read"], 1100)
        self.assertIn("unknown tool", src, "a result whose call was not seen")

    def test_mcp_tools_fold_to_one_row_and_odd_names_to_other(self):
        ts = stamp()
        self.write([reply("m1", ts, [use("a", "mcp__srv__one"), use("b", "mcp__srv__two"), use("c", "/etc/passwd x")]),
                    user(ts, result("a", 100), result("b", 100), result("c", 100))])
        msgs, ctx = self.scan()
        src = cost.context_weeks(msgs, ctx, {})[week(ts)]["sources"]
        self.assertGreaterEqual(src["MCP"], 200)
        self.assertGreaterEqual(src["other"], 100)
        self.assertFalse([k for k in src if "mcp__" in k or "/" in k])

    def test_tool_call_inputs_reminders_and_user_text_are_their_own_sources(self):
        ts = stamp()
        self.write([reply("m1", ts, [use("a", "Write", file_path="p", content="y" * 700)]),
                    user(ts, {"type": "text", "text": "<system-reminder>" + "r" * 300 + "</system-reminder>"},
                         {"type": "text", "text": "u" * 120}),
                    {"type": "attachment", "timestamp": ts, "attachment": {
                        "type": "hook_additional_context", "content": ["n" * 250]}},
                    {"type": "attachment", "timestamp": ts, "attachment": {"type": "skill_listing", "content": "s" * 90}}])
        msgs, ctx = self.scan()
        src = cost.context_weeks(msgs, ctx, {})[week(ts)]["sources"]
        self.assertGreaterEqual(src["tool-call inputs"], 700)
        self.assertGreaterEqual(src["reminders"], 300)
        self.assertGreaterEqual(src["user text"], 120)
        self.assertLess(src["user text"], 200, "the reminder is not user text")
        self.assertEqual(src["hook notes"], 250)
        self.assertGreaterEqual(src["attachment: skill_listing"], 90)

    def test_a_typed_prompt_arrives_as_a_string_and_a_reply_text_is_its_own_row(self):
        ts = stamp()
        self.write([{"type": "user", "timestamp": ts, "message": {"content": "u" * 80}},
                    {"type": "user", "timestamp": ts, "message": {"content": "<system-reminder>" + "r" * 60}},
                    reply("m1", ts, [{"type": "text", "text": "t" * 40}, {"type": "thinking", "thinking": "k" * 500}])])
        msgs, ctx = self.scan()
        src = cost.context_weeks(msgs, ctx, {})[week(ts)]["sources"]
        self.assertEqual((src["user text"], src["reply text"]), (80, 40))
        self.assertEqual(src["reminders"], 77)

    def test_a_split_reply_counts_its_tool_call_inputs_once(self):
        ts = stamp()
        block = use("a", "Write", content="y" * 500)
        self.write([reply("m1", ts, [block]), reply("m1", ts, [block])])
        msgs, ctx = self.scan()
        self.assertLess(cost.context_weeks(msgs, ctx, {})[week(ts)]["sources"]["tool-call inputs"], 600)

    def test_results_over_6000_characters_are_counted(self):
        ts = stamp()
        self.write([reply("m1", ts, [use("a", "Bash", command="x")]), user(ts, result("a", 7000))])
        msgs, ctx = self.scan()
        self.assertEqual(cost.context_weeks(msgs, ctx, {})[week(ts)]["big"], 1)

    def test_a_week_with_no_closed_task_prints_a_dash(self):
        ts = stamp()
        self.write([reply("m1", ts, [])])
        msgs, ctx = self.scan()
        got = cost.context_weeks(msgs, ctx, {})[week(ts)]
        self.assertIsNone(got["replies_per_closed"])
        self.assertIn("replies per closed task -", cost.context_text({week(ts): got}))
        got = cost.context_weeks(msgs, ctx, {week(ts): 4})[week(ts)]
        self.assertEqual(got["replies_per_closed"], 0.25)

    def test_an_unreadable_or_half_written_line_is_skipped(self):
        ts = stamp()
        self.write([reply("m1", ts, [use("a", "Bash", command="x")]), {"type": "user", "timestamp": ts, "message": 7},
                    {"type": "user", "timestamp": ts, "message": {"content": [3, None, {"type": "tool_result"}]}},
                    {"type": "attachment", "timestamp": ts, "attachment": "nope"}],
                   raw='{"type": "user", "timestamp": "' + ts + '", "mess\n')
        msgs, ctx = self.scan()
        self.assertEqual(cost.context_weeks(msgs, ctx, {})[week(ts)]["replies"], 1)

    def test_the_output_carries_numbers_and_tool_names_only(self):
        ts = stamp()
        secret = "SECRETWORDS-in-the-transcript"
        self.write([reply("m1", ts, [use("a", "Bash", command=secret), {"type": "text", "text": secret}]),
                    user(ts, {"type": "tool_result", "tool_use_id": "a", "content": secret},
                         {"type": "text", "text": secret}),
                    {"type": "attachment", "timestamp": ts, "attachment": {"type": secret, "content": secret}}])
        msgs, ctx = self.scan()
        weeks = cost.context_weeks(msgs, ctx, {})
        self.assertNotIn("SECRET", json.dumps(weeks) + cost.context_text(weeks))

    def test_closed_tasks_are_counted_by_the_week_of_their_close(self):
        self.fm("init")
        p = c.find_project(self.repo)
        ts = stamp(1)
        for tid, at, extra in (("T-0901", ts, ""), ("T-0902", ts, ""), ("T-0903", stamp(9), ""),
                               ("T-0904", ts, "done_in: T-0901\n")):
            c.save_brief(p, c.Brief.parse(f"---\nid: {tid}\ntype: FEATURE\ntier: M\nstatus: done\n{extra}---\n# t\n\n"
                                          f"## Log\n- {at} done\n"))
        c.save_brief(p, c.Brief.parse("---\nid: T-0905\ntype: FEATURE\ntier: M\nstatus: active\n---\n# t\n\n"
                                      f"## Log\n- {ts} done\n"))
        got = cost.closed_by_week(p)
        self.assertEqual(got[week(ts)], 2, "a folded task and an open one do not count")
        self.assertEqual(got[week(stamp(9))], 1)

    def test_sampo_cost_prints_the_section_and_keeps_its_lines(self):
        self.fm("init")
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        ts = stamp()
        with open(os.path.join(folder, "s1.jsonl"), "w") as f:
            f.write("".join(json.dumps(x) + "\n" for x in [
                reply("m1", ts, [use("a", "Bash", command="ls")], cache=2000),
                user(ts, result("a", 9000))]))
        env = {"CLAUDE_CONFIG_DIR": cfg}
        out = self.fm("cost", env=env).stdout
        self.assertIn("By task:", out)
        self.assertIn("Tool results (what filled the context):", out)
        self.assertIn("Where the context comes from", out)
        self.assertIn(week(ts), out)
        self.assertIn("cache reads 2k/reply", out)
        self.assertIn("1 over 6000 chars", out)
        data = self.fm_json("cost", env=env)
        self.assertEqual(data["context"][week(ts)]["replies"], 1)
        self.assertIn("tokens", data)
