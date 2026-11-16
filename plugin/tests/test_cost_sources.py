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


def note(ts, body, origin=None):
    """A background-task notification as the harness writes it: a user line whose origin says so."""
    return {"type": "user", "timestamp": ts, "origin": origin or {"kind": "task-notification", "producer": "session-task"},
            "message": {"content": "<task-notification>" + body + "</task-notification>"}}


AGENT = '<task-id>a1</task-id><status>completed</status><summary>Agent "SECRETNAME" {what}</summary>' \
        "<result>SECRETRESULT is waiting on a background run</result><usage>u</usage><subagent_tokens>9</subagent_tokens>"
SHELL = '<task-id>b1</task-id><output-file>f</output-file><status>completed</status><summary>Background command ' \
        '"SECRETNAME" completed (exit code 0)</summary>'
MONITOR = '<task-id>b2</task-id><event>SECRETEVENT</event><summary>Monitor event: "SECRETNAME"</summary>'
HUMAN = {"type": "user", "timestamp": "", "origin": {"kind": "human"}, "message": {"content": "SECRETTYPED"}}


def stop(ts, event="Stop"):
    return {"type": "attachment", "timestamp": ts, "attachment": {
        "type": "hook_blocking_error", "hookEvent": event, "blockingError": "SECRETBLOCK"}}


def at(rec, ts):
    return dict(rec, timestamp=ts)


class TurnTriggers(ForemanTestCase):
    """T-4815: each main-thread turn is counted by what started it, from the transcript's own record fields; only the
    agent-waiting-versus-finished split reads a harness summary's words."""

    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.tmp, "transcripts")
        os.makedirs(self.folder)

    def write(self, lines, name="s1.jsonl"):
        with open(os.path.join(self.folder, name), "a") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))

    def kinds(self, days=30):
        msgs, _ = cost.scan(self.folder, cost._since(days), sidechains=False, kinds=True)
        return [m[3] for m in sorted(msgs, key=lambda m: m[0])]

    def run_of(self, *gaps):
        """One reply after each gap (a list of records between two replies); timestamps rise a second per record."""
        lines, n = [], 0
        for i, gap in enumerate(gaps):
            for rec in gap:
                n += 1
                lines.append(at(rec, stamp().replace(".000Z", f".{n:03d}Z")))
            n += 1
            lines.append(reply(f"m{i}", stamp().replace(".000Z", f".{n:03d}Z"), []))
        self.write(lines)

    def test_every_kind_of_trigger_is_named_from_the_records(self):
        ts = stamp()
        self.run_of(
            [HUMAN],
            [user(ts, result("a", 5))],
            [note(ts, AGENT.format(what="finished"))],
            [note(ts, AGENT.format(what="is waiting on 2 background tasks"))],
            [note(ts, SHELL)],
            [note(ts, MONITOR)],
            [stop(ts), {"type": "user", "timestamp": ts, "isMeta": True, "message": {"content": "SECRETBLOCK"}}],
            [{"type": "system", "subtype": "compact_boundary", "timestamp": ts}],
            [])
        self.assertEqual(self.kinds(), ["user", "tool result", "agent finished", "agent waiting", "shell finished",
                                        "monitor event", "stop hook", "compaction", "other"])

    def test_only_the_summary_decides_waiting_never_the_result(self):
        self.run_of([note(stamp(), AGENT.format(what="finished"))])
        self.assertEqual(self.kinds(), ["agent finished"], "the result says waiting; the summary says finished")

    def test_an_agent_result_quoting_a_wrapper_tag_stays_an_agent(self):
        body = AGENT.format(what="finished").replace("SECRETRESULT", "<event>x</event>")
        self.run_of([note(stamp(), body)], [note(stamp(), MONITOR.replace("</summary>", "</summary><usage>u</usage>"))])
        self.assertEqual(self.kinds(), ["agent finished", "monitor event"])

    def test_a_typed_prompt_without_an_origin_is_a_user_turn_and_a_meta_line_is_not(self):
        ts = stamp()
        self.run_of([{"type": "user", "timestamp": ts, "message": {"content": "SECRETTYPED"}}],
                    [{"type": "user", "timestamp": ts, "isMeta": True, "message": {"content": "SECRETMETA"}}])
        self.assertEqual(self.kinds(), ["user", "other"])

    def test_a_post_tool_hook_error_is_not_a_stop_hook_continuation(self):
        ts = stamp()
        self.run_of([stop(ts, "PostToolUse"), user(ts, result("a", 5))])
        self.assertEqual(self.kinds(), ["tool result"])

    def test_a_person_beats_a_tool_result_and_a_notification_beats_one_too(self):
        ts = stamp()
        self.run_of([user(ts, result("a", 5)), HUMAN], [user(ts, result("a", 5)), note(ts, SHELL)])
        self.assertEqual(self.kinds(), ["user", "shell finished"])

    def test_a_reply_split_over_lines_has_the_trigger_of_its_first_line(self):
        ts = stamp()
        self.write([at(HUMAN, ts), reply("m1", ts, [use("a", "Bash", command="x")]), stop(ts), reply("m1", ts, [])])
        self.assertEqual(self.kinds(), ["user"])

    def test_the_first_record_of_each_file_starts_a_fresh_gap(self):
        ts = stamp()
        self.write([at(HUMAN, ts)], "a.jsonl")
        self.write([reply("m9", ts, [])], "b.jsonl")
        self.assertEqual(self.kinds(), ["other"], "the person's line was another file's")

    def test_odd_shapes_count_as_other_and_never_raise(self):
        ts = stamp()
        self.run_of([{"type": "user", "origin": 7, "message": {"content": [None, 3]}}],
                    [{"type": "user", "origin": {"kind": "task-notification"}, "message": {"content": 5}}],
                    [{"type": "attachment", "attachment": "nope"}, {"type": "system", "subtype": None}])
        self.assertEqual(self.kinds(), ["other", "other notification", "other"])

    def test_rows_sum_turns_and_cache_reads_by_kind(self):
        ts = stamp()
        self.write([at(HUMAN, ts), reply("m1", ts, [], cache=1000), user(ts, result("a", 5)),
                    reply("m2", ts, [], cache=3000), user(ts, result("a", 5)), reply("m3", ts, [], cache=5000)])
        msgs, _ = cost.scan(self.folder, cost._since(30), sidechains=False, kinds=True)
        rows = cost.trigger_rows(msgs)
        self.assertEqual(rows["user"], {"turns": 1, "cache_read": 1000})
        self.assertEqual(rows["tool result"], {"turns": 2, "cache_read": 8000})

    def test_sessions_print_and_export_the_kinds_with_numbers_only(self):
        self.fm("init")
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder)
        ts = stamp()
        with open(os.path.join(folder, "s1.jsonl"), "w") as f:
            f.write("".join(json.dumps(x) + "\n" for x in [
                at(HUMAN, ts), reply("m1", ts, [], cache=1000),
                note(ts, AGENT.format(what="is waiting on 1 background task")), reply("m2", ts, [], cache=2000),
                note(ts, MONITOR), reply("m3", ts, [], cache=3000),
                stop(ts), reply("m4", ts, [], cache=4000),
                user(ts, result("a", 5)), reply("m5", ts, [], cache=5000)]))
        env = {"CLAUDE_CONFIG_DIR": cfg}
        data = self.fm_json("cost", "--sessions", env=env)
        s = data["sessions"][0]["triggers"]
        self.assertEqual(s["agent waiting"], {"turns": 1, "cache_read": 2000})
        self.assertEqual(s["tool result"], {"turns": 1, "cache_read": 5000})
        self.assertEqual(data["by_trigger"]["monitor event"]["cache_read"], 3000)
        self.assertEqual(data["largest_avoidable"]["kind"], "stop hook")
        self.assertEqual(data["largest_avoidable"]["share_of_cache_read"], round(100 * 4000 / 15000))
        out = self.fm("cost", "--sessions", env=env).stdout
        self.assertIn("Turns by what started them", out)
        self.assertIn("stop hook 1 turn", out)
        self.assertIn("Largest avoidable kind: stop hook", out)
        self.assertNotIn("SECRET", out + json.dumps(data))
