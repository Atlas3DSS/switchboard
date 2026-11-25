"""Project memory (written when a conversation ends, read with sb memory) and retention (what the hub keeps)."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SB_STATE"] = tempfile.mkdtemp(prefix="sbtest-")
os.environ["SB_WORKSPACE"] = tempfile.mkdtemp(prefix="sbws-")

from switchboard import barrier, discovery, memory, retention  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import STATE, Store, TURNS, iso_from_epoch, now  # noqa: E402

ACCOUNTS = [{"name": "main", "kind": "claude", "email": "", "config_dir": ""},
            {"name": "oai", "kind": "codex", "email": "", "config_dir": ""}]
FAKE = r"""#!/usr/bin/env python3
import json, os, re, sys
prompt = sys.stdin.read()
argv = sys.argv[1:]
system = argv[argv.index("--system-prompt") + 1]
kind = "outline" if "outline one conversation" in system else "work" if "work queue honest" in system and "OUTLINE #" in prompt \
    else "sweep" if "work queue honest" in system else "memory"
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"kind": kind, "argv": argv, "prompt": prompt, "agent": os.environ.get("SB_AGENT")}) + "\n")
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "crash":
    sys.exit("boom")
if kind == "outline":
    msgs = re.findall(r"^\[(\d+)\] \S+ \S+ (OWNER|AGENT): (.*)$", prompt, re.M)
    def pts(who):
        return [[who.lower() + ' said "' + t[:40] + '"', n, " ".join(t.split()[:6])] for n, w, t in msgs if w == who][:2]
    asked, done = pts("OWNER"), pts("AGENT")
    if mode == "wrongref":
        asked[0][1] = msgs[-1][0]
    done.append(["deployed to production", msgs[0][0], "we deployed it to production yesterday"])
    def sec(head, points):
        return "## " + head + "\n" + "\n".join('- t: %s\n  m: %s\n  q: "%s"' % tuple(p) for p in points)
    reply = "I could not do that." if mode == "garbage" else "GIST: Parser work, %d messages.\n%s\n%s\n" % (
        len(msgs), sec("ASKED", asked), sec("DONE", done))
elif kind in ("work", "sweep"):
    ids = re.findall(r"^#(\d+) \[", prompt, re.M)
    if kind == "work":
        pts = re.findall(r"^P(\d+) \((\w+)\)", prompt, re.M)
        done = next(k for k, h in pts if h == "DONE")
        asked = next(k for k, h in pts if h == "ASKED")
        reply = "#%s done P%s\n#%s done P%s\n#9999 done P%s\n" % (ids[0], done, ids[1], asked, done)  # 2nd, 3rd: not supported
    else:
        now_l = re.search(r"^\[NOW\] L(\d+)", prompt, re.M).group(1)
        open_l = re.search(r"^\[OPEN\] L(\d+)", prompt, re.M).group(1)
        reply = "#%s done L%s\n#%s done L%s\n" % (ids[0], now_l, ids[1], open_l)
else:
    eid = re.search(r"OUTLINE #(\d+)", prompt).group(1)
    current = prompt.split("CURRENT MEMORY:\n", 1)[1].split("\n\nOUTLINE #", 1)[0]
    kept = [x for x in current.splitlines() if x.startswith("- ")]
    notes = re.findall(r"^n(\d+) \(", prompt, re.M)
    reply = "" if mode == "empty" else "\n".join(
        ["Here is the memory:", "## NOW (what the project is in the middle of)"] + kept + ["- parser fixed in src/parse.py %s[#%s]" % ("again " * len(kept), eid),
         "- the moon is made of cheese", "- invented source [#9999]"] + ["- a note says so [n%s]" % n for n in notes]
        + ["**OPEN:**", "- benchmarks %s[#%s, #9999]" % ("again " * len(kept), eid), "WHERE THINGS ARE"])
print(json.dumps({"is_error": False, "result": reply, "total_cost_usd": 0.002,
                  "usage": {"input_tokens": 5, "cache_creation_input_tokens": 700, "output_tokens": 90}}))
"""


def line(role, text, ts="2026-11-01T10:00:00Z"):
    msg = {"role": role, "content": text if role == "user" else [{"type": "text", "text": text}]}
    if role == "assistant":
        msg["model"] = "claude-opus-5-5"
    return json.dumps({"type": role, "timestamp": ts, "message": msg}) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "claude")
        os.makedirs(os.path.join(self.root, "-p"))
        for p in (mock.patch.multiple(discovery, CLAUDE_PROJECTS=self.root, EXTRA_PROJECTS=[]),
                  mock.patch.object(discovery, "accounts", return_value=ACCOUNTS)):
            p.start()
            self.addCleanup(p.stop)
        fake = os.path.join(self.tmp, "fake-claude")
        with open(fake, "w") as f:
            f.write(FAKE)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        env = mock.patch.dict(os.environ, {"SB_CLAUDE_BIN": fake, "FAKE_LOG": self.log, "FAKE_MODE": "ok",
                                           "SB_ANTIGRAVITY_BIN": os.path.join(self.tmp, "no-agy"),  # never the live CLIs
                                           "SB_CODEX_BIN": os.path.join(self.tmp, "no-codex")})
        env.start()
        self.addCleanup(env.stop)
        self.hub = Hub(Store(os.path.join(self.tmp, "hub.db")))
        for pid in ("alpha", "beta"):
            self.hub.s.upsert_project({"id": pid, "name": pid})

    def path(self, sid):
        return os.path.join(self.root, "-p", sid + ".jsonl")

    def agent(self, aid, pid, turns=6, extra="", **kw):
        sid = str(uuid.uuid4())
        with open(self.path(sid), "w") as f:
            for i in range(turns):
                f.write(line("user", f"ask {i} of {aid}: {extra}" + "please look at the parser " * 12))
                f.write(line("assistant", f"reply {i} of {aid}: " + "the parser is in src/parse.py " * 12))
        self.hub.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "main",
                              "mode": "terminal", "session_id": sid, "cwd": self.tmp, "state": "idle",
                              "model": "claude-opus-5-5", **kw})
        return sid

    def calls(self, kind=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [c for c in map(json.loads, f) if not kind or c["kind"] == kind]

    def end(self, aid, why="cleared"):
        a = self.hub.s.agent(aid)
        return memory.session_ended(self.hub, a, a["session_id"], why)

    def text(self, viewer="a1"):
        return memory.view(self.hub, None, viewer)["memory"]


class MemoryTests(Base):
    def test_cleared_conversation_becomes_a_checked_outline_and_cited_memory(self):
        sid = self.agent("a1", "alpha")
        eid = self.end("a1")
        self.assertEqual(self.hub.project_context(None, "a1")["memory"], {"updated_at": None, "sessions": 1})
        done = memory.tick(self.hub)
        self.assertEqual((done["state"], done["in_tokens"], done["out_tokens"], done["messages"]), ("done", 1410, 180, 12))
        call = self.calls("outline")[0]
        self.assertIsNone(call["agent"])  # the model is nobody's agent
        self.assertIn("claude-haiku-5-5", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", call["argv"])
        self.assertIn("[1] 2026-11-01 10:00 OWNER: ask 0 of a1", call["prompt"])
        x = memory.entry(self.hub, eid, "a1")
        secs = {sec["h"]: sec["points"] for sec in x["outline"]["sections"]}
        self.assertEqual([p["m"] for p in secs["ASKED"]], [[1], [3]])
        self.assertEqual(secs["ASKED"][0]["q"], "ask 0 of a1: please look")
        self.assertEqual(len(secs["DONE"]), 2)  # the point whose quote is nowhere in the transcript is gone
        self.assertEqual(x["outline"]["dropped"], 1)
        self.assertIn("1 points left out", x["note"])
        self.assertTrue(x["path"].endswith(sid + ".jsonl"))
        text = self.text()
        self.assertIn("CURRENT MEMORY:\n(empty)", self.calls("memory")[0]["prompt"])
        self.assertIn(f"- parser fixed in src/parse.py [#{eid}]", text)
        self.assertIn(f"- benchmarks [#{eid}]", text)  # the source that does not exist is taken off the line
        for bad in ("moon", "invented source", "Here is the memory", "9999", "WHERE THINGS ARE"):
            self.assertNotIn(bad, text)
        self.assertIn("3 memory lines left out", x["note"])
        self.assertEqual(memory.view(self.hub, None, "a1")["sessions"][0]["summary"], "Parser work, 12 messages.")
        self.assertIsNone(memory.tick(self.hub))  # nothing queued: nothing runs

    def test_a_point_leads_back_to_its_messages_in_full(self):
        self.agent("a1", "alpha")
        eid = self.end("a1")
        memory.tick(self.hub)
        src = memory.entry(self.hub, eid, "a1", at=3, around=1)["source"]
        self.assertEqual([x[:3] for x in src.split("\n\n")], ["[2]", "[3]", "[4]"])
        self.assertIn("ask 1 of a1: " + "please look at the parser " * 11, src)
        self.assertIn("SAID: reply 5 of a1", memory.entry(self.hub, eid, "a1", tail=2)["tail"])
        with self.assertRaises(LookupError):
            memory.entry(self.hub, eid, "a1", at=99)

    def test_a_quote_cited_to_the_wrong_message_is_moved_to_the_right_one(self):
        self.agent("a1", "alpha")
        os.environ["FAKE_MODE"] = "wrongref"
        eid = self.end("a1")
        memory.tick(self.hub)
        self.assertEqual(memory.entry(self.hub, eid, "a1")["outline"]["sections"][0]["points"][0]["m"], [1])

    def test_second_conversation_is_given_the_memory_so_far_and_keeps_its_sources(self):
        self.agent("a1", "alpha")
        self.agent("a2", "alpha")
        e1, e2 = self.end("a1"), self.end("a2")
        memory.tick(self.hub)
        memory.tick(self.hub)
        self.assertIn(f"CURRENT MEMORY:\nNOW\n- parser fixed in src/parse.py [#{e1}]", self.calls("memory")[1]["prompt"])
        text = self.text()
        self.assertIn(f"- parser fixed in src/parse.py [#{e1}]", text)
        self.assertIn(f"again [#{e2}]", text)
        self.assertEqual(self.hub.s.one("SELECT sessions FROM memory WHERE project='alpha'")["sessions"], 2)
        self.assertEqual(len(memory.versions(self.hub, "alpha")["versions"]), 1)

    def test_scope_is_the_project(self):
        self.agent("a1", "alpha")
        self.agent("b1", "beta")
        eid = self.end("a1")
        memory.tick(self.hub)
        with self.assertRaises(PermissionError):
            memory.view(self.hub, "alpha", "b1")
        with self.assertRaises(PermissionError):
            memory.entry(self.hub, eid, "b1", at=1)
        self.assertEqual(self.text("b1"), "")
        self.hub.s.set_reads("beta", ["alpha"])  # the owner's read grant covers memory, like context
        self.assertIn("parser fixed", memory.view(self.hub, "alpha", "b1")["memory"])

    def test_short_conversation_costs_nothing(self):
        self.agent("a1", "alpha", turns=1)
        self.end("a1")
        self.assertEqual(memory.tick(self.hub)["state"], "skipped")
        self.assertEqual(self.calls(), [])
        self.assertEqual(memory.view(self.hub, None, "a1")["sessions"], [])

    def test_daily_cap_and_off_switch_leave_a_pointer_without_a_call(self):
        sid = self.agent("a1", "alpha")
        self.agent("a2", "alpha")
        memory.set_settings(self.hub.s, {"daily_cap": 0})
        self.end("a1")
        row = memory.tick(self.hub)
        self.assertEqual((row["state"], row["summary"]), ("capped", None))
        self.assertTrue(row["first_ask"].startswith("ask 0 of a1"))
        self.assertTrue(row["last_said"].startswith("reply 5 of a1"))
        memory.set_settings(self.hub.s, {"daily_cap": 60})
        with open(self.path(sid), "a") as f:
            f.write(line("user", "one more thing"))
        self.end("a1")  # what the cap left unread is read with the next part
        memory.tick(self.hub)
        self.assertIn("[1] 2026-11-01 10:00 OWNER: ask 0 of a1", self.calls("outline")[0]["prompt"])
        memory.set_settings(self.hub.s, {"enabled": False})
        self.assertIsNone(self.end("a2"))  # off: nothing is even queued

    def test_failed_call_is_not_retried_and_keeps_the_memory(self):
        for aid in ("a1", "a2", "a3"):
            self.agent(aid, "alpha")
        self.end("a1")
        memory.tick(self.hub)
        before = self.text()
        for mode, aid in (("crash", "a2"), ("garbage", "a3")):
            os.environ["FAKE_MODE"] = mode
            self.end(aid)
            self.assertEqual(memory.tick(self.hub)["state"], "failed")
            self.assertIsNone(memory.tick(self.hub))
        self.assertEqual(self.text(), before)
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual(memory.calls_today(self.hub.s), 2)  # the garbage reply was paid for; the crash was not

    def test_empty_rewrite_never_wipes_and_the_owner_can_correct_and_go_back(self):
        self.agent("a1", "alpha")
        self.agent("a2", "alpha")
        self.end("a1")
        memory.tick(self.hub)
        first = self.text()
        os.environ["FAKE_MODE"] = "empty"
        self.end("a2")
        row = memory.tick(self.hub)
        self.assertEqual((row["state"], self.text()), ("done", first))
        self.assertIn("memory left as it was", row["note"])
        memory.set_text(self.hub, "alpha", "NOW\n- corrected by hand")
        self.assertEqual(self.text(), "NOW\n- corrected by hand")
        memory.set_text(self.hub, "alpha", restore=True)
        self.assertEqual(self.text(), first)
        vs = memory.versions(self.hub, "alpha")["versions"]
        self.assertEqual([v["text"] for v in vs], ["NOW\n- corrected by hand", first])
        memory.set_text(self.hub, "alpha", restore=vs[0]["id"])
        self.assertEqual(self.text(), "NOW\n- corrected by hand")
        memory.set_settings(self.hub.s, {"versions": 2})
        for i in range(5):
            memory.set_text(self.hub, "alpha", f"NOW\n- edit {i}")
        self.assertEqual([v["text"] for v in memory.versions(self.hub, "alpha")["versions"]],
                         ["NOW\n- edit 3", "NOW\n- edit 2"])

    def test_hand_written_lines_survive_a_rewrite_without_sources(self):
        self.agent("a1", "alpha")
        memory.set_text(self.hub, "alpha", "NOW\n- the owner wrote this line himself")
        self.end("a1")
        memory.tick(self.hub)
        self.assertIn("- the owner wrote this line himself", self.text())

    def test_resumed_conversation_sends_only_what_was_added(self):
        sid = self.agent("a1", "alpha")
        self.end("a1")
        memory.tick(self.hub)
        self.assertIsNone(self.end("a1"))  # nothing new since
        with open(self.path(sid), "a") as f:
            for i in range(6):
                f.write(line("user", f"later ask {i}: " + "now the benchmarks " * 12))
                f.write(line("assistant", f"later reply {i}: " + "41 tok/s on the new build " * 12))
        self.end("a1")
        memory.tick(self.hub)
        prompt = self.calls("outline")[1]["prompt"]
        self.assertIn("[1] 2026-11-01 10:00 OWNER: later ask 0", prompt)
        self.assertNotIn("ask 0 of a1", prompt)
        self.assertIn("EARLIER PART OF THIS CONVERSATION, already outlined: Parser work, 12 messages.", prompt)

    def test_long_conversation_is_read_in_pieces_and_says_what_it_skipped(self):
        def entries(n):
            return [("user" if i % 2 == 0 else "assistant", f"m{i} " + "x" * 900, "2026-11-01T10:00:00Z") for i in range(n)]
        pieces, unread = memory.pack(entries(100))
        self.assertEqual((len(pieces), unread), (2, None))
        self.assertEqual(sum(len(p) for p in pieces), 100)
        pieces, unread = memory.pack(entries(400))
        self.assertEqual(len(pieces), memory.MAX_CHUNKS)
        self.assertTrue(all(sum(len(x) + 1 for x in p) <= memory.DIGEST_CAP for p in pieces))
        self.assertTrue(pieces[0][0].startswith("[1] ") and pieces[-1][-1].startswith("[400] "))
        first_tail = int(pieces[1][0][1:pieces[1][0].index("]")])
        self.assertEqual(unread, (len(pieces[0]) + 1, first_tail - 1))
        self.assertEqual(pieces[1][-1].split("]")[0], "[" + str(int(pieces[2][0][1:pieces[2][0].index("]")]) - 1))

    def test_secrets_never_reach_the_model_or_the_memory(self):
        key = "sk-ant-api03-" + "A1b2C3d4" * 6
        self.agent("a1", "alpha", extra=f"my key is {key} and DB_PASSWORD=hunter2hunter2X9 ")
        self.hub.add_note({"from": "a1", "text": f"deploy with token: ghp_{'a1B2' * 10}"})
        eid = self.end("a1")
        memory.tick(self.hub)
        sent = "".join(c["prompt"] for c in self.calls())
        stored = json.dumps(self.hub.s.q("SELECT * FROM memory_sessions")) + self.text()
        for where in (sent, stored, memory.entry(self.hub, eid, "a1", at=1)["source"]):
            self.assertNotIn(key, where)
            self.assertNotIn("hunter2", where)
            self.assertNotIn("ghp_", where)
        self.assertIn("[redacted: api key]", sent)
        self.assertIn("DB_PASSWORD=[redacted: db_password]", sent)
        for harmless in ("max_tokens=4096", "token=/home/main/.config/x/token.json", "in_tokens: 2934",
                         "the password is in the vault", "commit 3460eda1b2c3d4e5f6"):
            self.assertEqual(memory.redact(harmless), harmless)

    def test_new_notes_are_given_to_the_rewrite_once(self):
        self.agent("a1", "alpha")
        self.agent("a2", "alpha")
        self.hub.add_note({"from": "a1", "text": "owner rule: never deploy on Fridays"})
        nid = self.hub.s.one("SELECT id FROM notes")["id"]
        self.end("a1")
        memory.tick(self.hub)
        self.assertIn(f"n{nid} (", self.calls("memory")[0]["prompt"])
        self.assertIn("owner rule: never deploy on Fridays", self.calls("memory")[0]["prompt"])
        self.assertIn(f"- a note says so [n{nid}]", self.text())
        self.end("a2")
        memory.tick(self.hub)
        self.assertIn("NEW NOTES:\n(none)", self.calls("memory")[1]["prompt"])
        self.assertIn(f"- a note says so [n{nid}]", self.text())  # its source stays valid: the memory already cites it

    def test_quiet_conversations_are_outlined_without_a_clear(self):
        quiet, busy = self.agent("a1", "alpha"), self.agent("a2", "alpha")
        long_ago = time.time() - 3 * 3600
        os.utime(self.path(quiet), (long_ago, long_ago))
        self.assertEqual(memory.idle_sweep(self.hub), 1)
        row = self.hub.s.one("SELECT * FROM memory_sessions")
        self.assertEqual((row["agent"], row["why"], row["session_id"]), ("a1", "idle for 2 h", quiet))
        self.assertEqual(memory.idle_sweep(self.hub), 0)  # already queued
        memory.tick(self.hub)
        self.assertEqual(memory.idle_sweep(self.hub), 0)  # nothing new in it since
        with open(self.path(quiet), "a") as f:
            f.write(line("user", "and one more question"))
        os.utime(self.path(quiet), (long_ago, long_ago))
        os.utime(self.path(busy), (long_ago, long_ago))
        memory.set_settings(self.hub.s, {"idle_hours": 0})
        self.assertEqual(memory.idle_sweep(self.hub), 0)  # the idle trigger is off
        memory.set_settings(self.hub.s, {"idle_hours": 2, "daily_cap": 1})
        self.assertEqual(memory.idle_sweep(self.hub), 0)  # the day's cap is spent: nothing is queued
        memory.set_settings(self.hub.s, {"daily_cap": 60})
        self.assertEqual(memory.idle_sweep(self.hub), 2)

    def test_helpers_and_unsorted_agents_are_left_out(self):
        self.agent("h1", "alpha", role="helper")
        self.agent("u1", "unsorted")
        self.assertIsNone(self.end("h1"))
        self.assertIsNone(self.end("u1"))

    def test_clear_in_the_terminal_queues_the_old_conversation(self):
        old = self.agent("a1", "alpha")
        new = str(uuid.uuid4())
        with mock.patch.object(self.hub.t, "status", return_value={"pid": 4242, "state": "idle"}):
            self.hub.follow_cleared([{"pid": 4242, "sessionId": new}])
        self.assertEqual(self.hub.s.agent("a1")["session_id"], new)
        row = self.hub.s.one("SELECT * FROM memory_sessions")
        self.assertEqual((row["session_id"], row["state"], row["project"]), (old, "pending", "alpha"))

    def test_cleared_conversations_stay_searchable(self):
        self.agent("a1", "alpha")
        self.end("a1")
        self.hub.s.set_agent("a1", session_id=None)
        hits = self.hub.context_search(None, "parser", "a1")["hits"]
        self.assertTrue(hits and "ended, sb memory" in hits[0]["agent"])

    def test_agents_may_read_memory_but_not_change_it_or_the_settings(self):
        for path in ("/api/memory", "/api/memory/12", "/api/projects/alpha/memory"):
            self.assertTrue(barrier.agent_may("GET", path))
        for method, path in (("GET", "/api/memory/usage"), ("GET", "/api/retention"), ("POST", "/api/retention/run"),
                             ("GET", "/api/projects/alpha/memory/versions"), ("POST", "/api/projects/alpha/memory"),
                             ("POST", "/api/settings")):
            self.assertFalse(barrier.agent_may(method, path))


class WriterTests(Base):
    """The memory is written by the first writer that can be reached (memory.settings 'writers')."""

    def call(self, **fakes):
        spent = {}
        row = {"provider": "claude", "account": "main", "project": "alpha"}
        with mock.patch.multiple(memory, **fakes):
            return memory._call(self.hub, row, "SYS", "PROMPT", memory.settings(self.hub.s), spent), spent

    def test_claude_out_of_reach_goes_to_the_next_writer(self):
        from switchboard import antigravity
        gone = mock.Mock(side_effect=RuntimeError("exit 1: OAuth token has expired, please log in"))
        with mock.patch.object(antigravity, "consult", return_value=("from gemini", {"in_tokens": "12", "out_tokens": "3"})) as c:
            text, spent = self.call(_call_claude=gone)
        self.assertEqual((text, spent["in_tokens"]), ("from gemini", 12))
        self.assertEqual(c.call_args[0][0], "gemini-3.8-flash-medium")
        self.assertIn("SYS", c.call_args[0][1][0])  # agy has no system prompt: the instructions lead the message

    def test_an_error_claude_answered_with_is_not_passed_on(self):
        from switchboard import antigravity
        with mock.patch.object(antigravity, "consult") as c:
            with self.assertRaises(RuntimeError):
                self.call(_call_claude=mock.Mock(side_effect=RuntimeError("prompt is too long")))
        c.assert_not_called()

    def test_each_writer_in_turn_and_the_first_failure_is_the_one_reported(self):
        gone = mock.Mock(side_effect=ConnectionError("no Claude account"))
        text, spent = self.call(_call_claude=gone, _call_antigravity=mock.Mock(side_effect=ConnectionError("quota")),
                                _call_codex=mock.Mock(return_value="from codex"))
        self.assertEqual(text, "from codex")
        with self.assertRaisesRegex(RuntimeError, "claude: no Claude account"):
            self.call(_call_claude=gone, _call_antigravity=mock.Mock(side_effect=ConnectionError("quota")),
                      _call_codex=mock.Mock(side_effect=OSError("no codex")))

    def test_writers_setting(self):
        cfg = memory.set_settings(self.hub.s, {"writers": "antigravity,codex", "writer_models": {"codex": "gpt-x"}})
        self.assertEqual(cfg["writers"], ["antigravity", "codex"])
        self.assertEqual(cfg["writer_models"], {"antigravity": "gemini-3.8-flash-medium", "codex": "gpt-x"})
        with self.assertRaises(ValueError):
            memory.set_settings(self.hub.s, {"writers": ["llama"]})


class WorkCloseTests(Base):
    """The queue closes itself: an outline's DONE point closes the item it reports on; the owner clicks nothing."""

    def items(self):
        from switchboard import fleet
        a = fleet.work_add(self.hub, {"title": "fix the parser", "project": "alpha", "from": "owner"})
        b = fleet.work_add(self.hub, {"title": "write the benchmarks", "project": "alpha", "from": "owner"})
        return a["id"], b["id"]

    def test_finished_work_is_closed_from_a_conversation_outline(self):
        from switchboard import fleet
        a, b = self.items()
        self.agent("a1", "alpha")
        self.end("a1")
        row = memory.tick(self.hub)
        self.assertEqual(row["state"], "done")
        self.assertIn(f"closed work #{a}", row["note"])
        wa, wb = fleet.work_item(self.hub, a), fleet.work_item(self.hub, b)
        self.assertEqual(wa["status"], "done")
        self.assertIn("closed by the hub from conversation #", wa["detail"])
        self.assertRegex(wa["detail"], r"agent said .*\[#1 m\d")  # the DONE point that shows it, with its source
        self.assertEqual(wb["status"], "todo", "cited an ASKED point: not finished work")
        self.assertEqual(len(self.calls("work")), 1)
        ev = [r["text"] for r in self.hub.s.q("SELECT text FROM events WHERE agent='a1'")]
        self.assertTrue(any(f"work #{a} marked done by the hub" in x for x in ev), ev)

    def test_the_backlog_is_swept_from_the_project_memory(self):
        from switchboard import fleet
        a, b = self.items()
        memory.store_memory(self.hub.s, "alpha", "NOW\n- parser fixed in src/parse.py [#1]\nOPEN\n- benchmarks still to run [#1]", "test")
        self.assertEqual(memory.sweep_work(self.hub, "alpha", "main"), [a])
        self.assertIn("closed by the hub from the project memory: - parser fixed", fleet.work_item(self.hub, a)["detail"])
        self.assertEqual(fleet.work_item(self.hub, b)["status"], "todo", "a line under OPEN never closes an item")
        self.assertEqual(memory.sweep_work(self.hub, "beta", "main"), [], "no memory, no call")


class RetentionTests(Base):
    def test_old_finished_rows_go_and_open_ones_stay(self):
        s = self.hub.s
        self.agent("a1", "alpha")
        old, new = iso_from_epoch(time.time() - 40 * 86400), now()
        for ts in (old, new):
            s.event("a1", "user", "hello", ts=ts)
        for state, at in (("done", old), ("queued", old), ("held", old), ("done", new)):
            s.x("INSERT INTO messages(id,idem,to_agent,from_agent,body,state,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"m-{state}-{at}", f"m-{state}-{at}", "a1", "owner", "x", state, at, at if state == "done" else None))
        for nid, level, acked in (("n1", "need", None), ("n2", "need", old), ("n3", "info", None)):
            s.x("INSERT INTO notifications(id,idem,agent,project,kind,level,text,created_at,acked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (nid, nid, "a1", "alpha", "notify", level, "t", old, acked))
        for jid, state in (("j1", "finished"), ("j2", "running")):
            s.x("INSERT INTO jobs(id,name,state,created_at,finished_at) VALUES(?,?,?,?,?)", (jid, jid, state, old, old))
        s.x("INSERT INTO decisions(id,project,question,state,created_at) VALUES('d1','alpha','q','answered',?)", (old,))
        s.x("INSERT INTO work(project,title,status,created_at) VALUES('alpha','t','done',?)", (old,))
        last = retention.sweep(self.hub)
        out = {k: v for k, v in last["removed"].items() if k not in ("turns", "pty")}  # folders are shared by the tests
        self.assertEqual(out, {"events": 1, "messages": 1, "notifications": 2, "jobs": 1})
        self.assertEqual({m["state"] for m in s.q("SELECT state FROM messages")}, {"done", "queued", "held"})
        self.assertEqual([n["id"] for n in s.q("SELECT id FROM notifications")], ["n1"])
        self.assertEqual([j["id"] for j in s.q("SELECT id FROM jobs")], ["j2"])
        self.assertEqual(s.one("SELECT COUNT(*) n FROM decisions")["n"] + s.one("SELECT COUNT(*) n FROM work")["n"], 2)
        self.assertEqual(self.hub.s.setting("retention_last"), last)

    def test_per_agent_event_cap_and_note_floor(self):
        s = self.hub.s
        retention.set_settings(s, {"events_per_agent": 10, "notes_keep": 3})
        for i in range(25):
            s.event("a1", "user", f"e{i}")
            s.event("a2", "user", f"f{i}") if i < 5 else None
        old = iso_from_epoch(time.time() - 400 * 86400)
        for i in range(8):
            s.x("INSERT INTO notes(project,agent,text,created_at) VALUES('alpha','a1',?,?)", (f"n{i}", old))
        out = retention.sweep(self.hub)["removed"]
        self.assertEqual((out["events"], out["notes"]), (15, 5))
        self.assertEqual([e["text"] for e in s.events("a1")][0], "e15")
        self.assertEqual(len(s.events("a2")), 5)
        self.assertEqual([n["text"] for n in s.q("SELECT text FROM notes ORDER BY id")], ["n5", "n6", "n7"])
        with self.assertRaises(ValueError):
            retention.set_settings(s, {"nonsense_days": 3})

    def test_memory_entries_are_capped_per_project(self):
        s = self.hub.s
        retention.set_settings(s, {"memory_sessions": 4})
        for i in range(9):
            s.x("INSERT INTO memory_sessions(project,agent,state,created_at) VALUES(?,?,?,?)",
                ("alpha" if i < 7 else "beta", "a1", "done", now()))
        s.x("INSERT INTO memory_sessions(project,agent,state,created_at) VALUES('alpha','a1','pending',?)", (now(),))
        oldest = s.one("SELECT MIN(id) n FROM memory_sessions")["n"]
        memory.store_memory(s, "alpha", f"NOW\n- still true [#{oldest}]", "test")
        self.assertEqual(retention.sweep(self.hub)["removed"].get("memory_sessions"), 3)
        self.assertEqual(s.one("SELECT COUNT(*) n FROM memory_sessions WHERE project='beta'")["n"], 2)
        self.assertTrue(s.one("SELECT 1 n FROM memory_sessions WHERE id=?", (oldest,)))  # the memory still cites it

    def test_old_files_go_unless_something_open_points_at_them(self):
        s = self.hub.s
        self.agent("a1", "alpha")
        s.x("INSERT INTO messages(id,idem,to_agent,from_agent,body,state,turn_id,created_at) VALUES('m1','m1','a1','owner','x','delivered','t-open',?)", (now(),))
        pty = os.path.join(STATE, "pty")  # the folder the code uses, whichever test module was imported first
        os.makedirs(pty, exist_ok=True)
        long_ago = time.time() - 30 * 86400
        made = {}
        for name, d, aged in (("t-open.jsonl", TURNS, True), ("t-old.jsonl", TURNS, True), ("t-new.prompt", TURNS, False),
                              ("a1.log", pty, True), ("ghost.log", pty, True), ("ghost.json", pty, True),
                              ("fresh-ghost.log", pty, False), ("notes.txt", pty, True)):
            made[name] = os.path.join(d, name)
            with open(made[name], "w") as f:
                f.write("{}")
            if aged:
                os.utime(made[name], (long_ago, long_ago))
        out = retention.sweep(self.hub)["removed"]
        self.assertEqual((out.get("turns"), out.get("pty")), (1, 2))
        left = {n for n, p in made.items() if os.path.exists(p)}
        self.assertEqual(left, {"t-open.jsonl", "t-new.prompt", "a1.log", "fresh-ghost.log", "notes.txt"})
        self.assertIn("hub_db_bytes", retention.view(self.hub)["sizes"])


if __name__ == "__main__":
    unittest.main()
