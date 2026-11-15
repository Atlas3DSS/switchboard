"""Usage insights: the per-transcript ledger (switchboard/insights.py) and the view the Insights page reads."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SB_STATE"] = tempfile.mkdtemp(prefix="sbtest-")

from switchboard import insights  # noqa: E402


def ts(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def claude_lines(t0):
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 9000,
             "output_tokens": 200, "output_tokens_details": {"thinking_tokens": 50},
             "cache_creation": {"ephemeral_1h_input_tokens": 1000, "ephemeral_5m_input_tokens": 0}}
    msg = lambda content: {"id": "msg_1", "model": "claude-opus-5-5", "usage": usage, "content": content}  # noqa: E731
    return [
        {"type": "user", "timestamp": ts(t0), "message": {"role": "user", "content": "fix the bug"}},
        {"type": "user", "timestamp": ts(t0 + 1), "message": {"content": "<system-reminder>x</system-reminder>"}},
        # one API message split over three lines that repeat its usage: counted once
        {"type": "assistant", "timestamp": ts(t0 + 5), "message": msg([{"type": "thinking", "thinking": ""}])},
        {"type": "assistant", "timestamp": ts(t0 + 6), "message": msg([{"type": "tool_use", "name": "Bash",
                                                                        "input": {"command": "cd x && git status | head -3\npython3 - <<'EOF'\nimport os\nEOF"}}])},
        {"type": "assistant", "timestamp": ts(t0 + 7), "message": msg([{"type": "tool_use", "name": "Skill", "input": {"skill": "dataviz"}}])},
        {"type": "assistant", "isSidechain": True, "timestamp": ts(t0 + 8), "message": {**msg([]), "id": "msg_side"}},
        {"type": "assistant", "timestamp": ts(t0 + 9), "message": {"id": "msg_2", "model": "<synthetic>", "usage": usage, "content": []}},
    ]


class Hub:
    def __init__(self, rows):
        self.rows = rows
        self.s = self

    def q(self, sql, args=()):
        return [] if "memory_sessions" in sql else self.rows

    def agent(self, aid):
        return {"id": aid, "title": "Tester", "model": "claude-opus-5-5", "provider": "claude", "gone": 0,
                "context_tokens": 5000, "context_window": 1000000}


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sbins-")
        self.path = os.path.join(self.dir, "s.jsonl")
        self.t0 = time.time() - 120
        with open(self.path, "w") as f:
            for e in claude_lines(self.t0):
                f.write(json.dumps(e) + "\n")
            f.write('{"type": "assistant", "partial')  # a line still being written: left for the next pass
        rows = [{"id": "a1", "project": "p1", "provider": "claude", "account": "main", "session_id": "s"}]
        self.hub = Hub(rows)
        insights.discovery.transcript_path = lambda prov, sid, acc: self.path

    def test_programs(self):
        self.assertEqual(insights.programs("cd x && git status | head -3"), ["git", "head"])
        self.assertEqual(insights.programs("python3 - <<'EOF'\nimport os\nfor x in y: pass\nEOF\nls -la"), ["python3", "ls"])
        self.assertEqual(insights.programs("FOO=1 sudo -E timeout 60 pytest -q; for f in *; do wc -l $f; done"),
                         ["pytest", "wc"])

    def test_price(self):
        self.assertEqual(insights.price_of("claude-opus-5-5"), (4.0, 20.0, 0.20))
        self.assertEqual(insights.price_of("claude-fable-5-1")[0], 10.0)
        self.assertIsNone(insights.price_of("gpt-6-astra"))

    def test_ledger_and_view(self):
        ins = insights.Insights(self.hub, path=os.path.join(self.dir, "cache.json"))
        ins.tick()
        v = ins.view("7d")
        k = v["kpi"]
        self.assertEqual(k["turns"], 1)  # the system reminder is not a turn
        self.assertEqual(k["requests"], 1)  # three lines of msg_1, sidechain and synthetic skipped
        self.assertEqual(k["billed"], 10 + 1000 + 200)
        self.assertAlmostEqual(k["cache_hit"], 9000 / 10010, places=4)
        # $4 in, 1 h cache write at 2x input, $0.20 cache read, $20 out
        self.assertAlmostEqual(k["cost"], round((10 * 4 + 1000 * 8 + 9000 * 0.2 + 200 * 20) / 1e6, 2))
        self.assertEqual(v["composition"]["reasoning"], 50)
        self.assertEqual({r["name"] for r in v["tools"]["rows"]}, {"Bash", "Skill"})
        self.assertEqual([r["name"] for r in v["commands"]["rows"]], ["git", "head", "python3"])
        self.assertEqual(v["skills"]["rows"][0]["name"], "dataviz")
        self.assertEqual(v["agents"][0]["id"], "a1")
        self.assertEqual(v["projects"][0]["id"], "p1")
        self.assertGreater(k["active_s"], 0)
        self.assertEqual(ins.view("7d", project="other")["kpi"]["billed"], 0)

        # appended lines are read incrementally, and the cache survives a restart
        with open(self.path, "a") as f:
            f.write('\n' + json.dumps({"type": "user", "timestamp": ts(self.t0 + 30),
                                       "message": {"content": "next"}}) + "\n")
        again = insights.Insights(self.hub, path=os.path.join(self.dir, "cache.json"))
        again.tick()
        self.assertEqual(again.view("7d")["kpi"]["turns"], 2)
        self.assertEqual(again.view("7d")["kpi"]["requests"], 1)


if __name__ == "__main__":
    unittest.main()


class TraceTests(unittest.TestCase):
    def test_trace_composition_and_compaction(self):
        from switchboard import trace
        d = tempfile.mkdtemp(prefix="sbtr-")
        path = os.path.join(d, "s.jsonl")
        t0 = time.time() - 600
        usage = lambda ctx: {"input_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 5,  # noqa: E731
                             "output_tokens": 50}
        lines = [
            {"type": "api-request-shape", "shape": {"system": [{"text": "s" * 3600}], "tools": [{"d": "t" * 7200}]}},
            {"type": "attachment", "timestamp": ts(t0), "attachment": {"type": "skill_listing"}, "rendered": [{"content": "k" * 3600}]},
            {"type": "user", "timestamp": ts(t0 + 1), "message": {"content": "u" * 360}},
            {"type": "user", "isMeta": True, "timestamp": ts(t0 + 1), "message": {"content": [{"type": "text", "text": "m" * 3600}]}},
            {"type": "assistant", "timestamp": ts(t0 + 4), "message": {"id": "m1", "model": "claude-opus-5-5", "usage": usage(10000),
                                                                       "content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "timestamp": ts(t0 + 6), "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "r" * 36000}]}},
            {"type": "assistant", "timestamp": ts(t0 + 9), "message": {"id": "m2", "model": "claude-opus-5-5", "usage": usage(30000),
                                                                       "content": [{"type": "text", "text": "done"}]}},
            {"type": "system", "subtype": "compact_boundary", "timestamp": ts(t0 + 20)},
            {"type": "user", "isCompactSummary": True, "timestamp": ts(t0 + 21), "message": {"content": "summary " * 100}},
            {"type": "assistant", "timestamp": ts(t0 + 25), "message": {"id": "m3", "model": "claude-opus-5-5", "usage": usage(8000), "content": []}},
        ]
        with open(path, "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        v = trace.Traces().view(path, "claude")
        self.assertEqual(v["requests"], 3)
        self.assertEqual(v["turns"], 1)  # the meta entry and the compact summary are not turns
        self.assertEqual(len(v["compactions"]), 1)
        self.assertEqual(v["compactions"][0]["at"], 2)
        second = v["series"][1]
        mix = dict(zip(v["cats"], second["mix"]))
        self.assertEqual(sum(mix.values()), 30000)  # fitted to the real total
        self.assertGreater(mix["tool_results"], mix["user"])
        self.assertGreater(mix["injected"], 0)  # the attachment and the meta entry
        self.assertEqual(v["current"]["ctx"], 8000)
        self.assertEqual(v["tool_time"][0]["name"], "Bash")
        self.assertAlmostEqual(v["tool_time"][0]["s"], 2.0, places=0)
        self.assertEqual(v["series"][0]["dur"], 3.0)  # from the last user line to the reply
        self.assertEqual(v["biggest"][0]["name"], "Bash")


class TeamTests(unittest.TestCase):
    def test_team_view(self):
        from switchboard import team
        from switchboard.store import Store, iso_from_epoch
        st = Store(os.path.join(tempfile.mkdtemp(prefix="sbteam-"), "hub.db"))
        st.db.execute("INSERT INTO agents (id, project, title, provider, model, gone) VALUES ('a1','p','Lead','claude','claude-opus-5-5',0)")
        st.db.execute("INSERT INTO agents (id, project, title, provider, gone) VALUES ('a2','p','Helper','codex',0)")
        st.db.execute("INSERT INTO agents (id, project, title, provider, gone) VALUES ('z9','other','Elsewhere','claude',0)")
        now = time.time()
        for i, (frm, to, rep) in enumerate((("a1", "a2", 0), ("a2", "a1", 1), (None, "a1", 0), ("z9", "z9", 0))):
            st.db.execute("INSERT INTO messages (id, to_agent, from_agent, body, state, is_reply, created_at) VALUES (?,?,?,?,?,?,?)",
                          (f"m{i}", to, frm, f"body {i}", "done", rep, iso_from_epoch(now - 60 * (5 - i))))
        st.db.execute("INSERT INTO messages (id, to_agent, from_agent, body, state, created_at) VALUES ('old','a1','a2','x','done',?)",
                      (iso_from_epoch(now - 3 * 86400),))
        st.db.execute("INSERT INTO notes (project, agent, text, created_at) VALUES ('p','a2','a note',?)", (iso_from_epoch(now - 30),))

        class H:
            s = st

            def lead_of(self, pid):
                return "a1"
        v = team.view(H(), "p", 24)
        self.assertEqual([e["kind"] for e in v["events"]], ["message", "reply", "message", "note"])
        self.assertEqual(v["events"][2]["from"], "owner")  # a message with no sender is the owner's
        a1 = next(a for a in v["agents"] if a["id"] == "a1")
        self.assertEqual((a1["sent"], a1["received"]), (1, 2))
        self.assertNotIn("z9", {a["id"] for a in v["agents"]})  # another project's traffic stays out
        self.assertEqual(v["lead"], "a1")
