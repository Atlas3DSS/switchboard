"""Past-session search and resume, clearing the Switchboard chat, following /clear inside a terminal."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SB_STATE"] = tempfile.mkdtemp(prefix="sbtest-")
os.environ["SB_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="sbcfg-"), "accounts.json")
os.environ["SB_WORKSPACE"] = tempfile.mkdtemp(prefix="sbws-")

from switchboard import discovery, sessions  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"))  # compact, like the CLIs write


def claude_line(text, ts, cwd, entry="cli", **kw):
    return dumps({"type": "user", "cwd": cwd, "timestamp": ts, "entrypoint": entry, "sessionId": "x",
                  "message": {"role": "user", "content": text}, **kw}) + "\n"


class SessionIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.claude_root = os.path.join(self.tmp, "claude")
        self.codex_root = os.path.join(self.tmp, "codex")
        self.work = os.path.join(self.tmp, "work", "web-ops")
        os.makedirs(self.work)
        os.makedirs(os.path.join(self.claude_root, "-proj"))
        os.makedirs(os.path.join(self.codex_root, "2026", "10", "30"))
        p = mock.patch.multiple(discovery, CLAUDE_PROJECTS=self.claude_root, CODEX_SESSIONS=self.codex_root,
                                EXTRA_PROJECTS=[])
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(sessions, "CODEX_INDEX", os.path.join(self.tmp, "session_index.jsonl"))
        q.start()
        self.addCleanup(q.stop)
        self.sid = "11111111-2222-3333-4444-555555555555"
        self.path = os.path.join(self.claude_root, "-proj", self.sid + ".jsonl")
        with open(self.path, "w") as f:
            f.write(claude_line("roll back the lantern config to yesterday", "2026-10-01T10:00:00Z", self.work))
            f.write(claude_line("<system-reminder>noise</system-reminder>", "2026-10-01T10:00:01Z", self.work))
            f.write(dumps({"type": "ai-title", "aiTitle": "Lantern rollback"}) + "\n")
        with open(os.path.join(self.claude_root, "-proj", "99999999-0000-0000-0000-000000000000.jsonl"), "w") as f:
            f.write(claude_line("grade these lantern answers", "2026-10-02T10:00:00Z", "/", entry="sdk-cli"))
        self.cid = "01a0aaaa-bbbb-7ccc-dddd-eeeeeeeeeeee"
        with open(os.path.join(self.codex_root, "2026", "10", "30", f"rollout-2026-10-30-{self.cid}.jsonl"), "w") as f:
            f.write(dumps({"type": "session_meta", "payload": {"id": self.cid, "cwd": self.work,
                                                                    "originator": "codex-tui"}}) + "\n")
            for kind in ("event_msg", "response_item"):  # Codex writes the same ask twice
                payload = ({"type": "user_message", "message": "update the build script"} if kind == "event_msg"
                           else {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "update the build script"}]})
                f.write(dumps({"type": kind, "timestamp": "2026-10-03T10:00:00Z", "payload": payload}) + "\n")
        with open(sessions.CODEX_INDEX, "w") as f:
            f.write(dumps({"id": self.cid, "thread_name": "Server update"}) + "\n")
        self.ix = sessions.Index(os.path.join(self.tmp, "sessions.db"))

    def test_search_finds_asks_and_titles_and_hides_headless(self):
        self.ix.refresh()
        hits = self.ix.search("lantern")
        self.assertEqual([h["id"] for h in hits], [self.sid])
        self.assertEqual(hits[0]["title"], "Lantern rollback")
        self.assertEqual(hits[0]["n_asks"], 1)
        self.assertEqual(len(self.ix.search("lantern", headless=True)), 2)
        x = self.ix.search("build scr")[0]
        self.assertEqual((x["id"], x["title"], x["n_asks"]), (self.cid, "Server update", 1))
        self.assertEqual(self.ix.search(self.sid[:8])[0]["id"], self.sid)

    def test_only_the_new_tail_is_read(self):
        self.ix.refresh()
        self.assertEqual(self.ix.refresh(), 0)
        with open(self.path, "a") as f:
            f.write(claude_line("now also rename the config loader", "2026-10-04T10:00:00Z", self.work))
            f.write('{"type":"user","partial')  # half-written line: left for the next refresh
        self.assertEqual(self.ix.refresh(), 1)
        x = self.ix.search("config loader")[0]
        self.assertEqual((x["id"], x["n_asks"], x["last_ts"]), (self.sid, 2, "2026-10-04T10:00:00Z"))
        self.assertEqual(self.ix.search("lantern rollback")[0]["id"], self.sid)  # older asks still searchable

    def test_resume_makes_project_then_reuses_agent(self):
        self.ix.refresh()
        hub = Hub(Store(os.path.join(tempfile.mkdtemp(), "hub.db")))
        hub._ix = self.ix
        with mock.patch.object(discovery, "accounts", return_value=[
                {"name": "main", "kind": "claude", "config_dir": ""},
                {"name": "chatgpt", "kind": "codex", "config_dir": ""}]):
            view = hub.search_sessions("lantern")["sessions"][0]
            self.assertEqual((view["agent"], view["project"]), (None, None))
            r = hub.resume_session({"session_id": self.sid})
            self.assertTrue(r["created"] and r["created_project"])
            a = r["agent"]
            self.assertEqual((a["session_id"], a["cwd"], a["project"]), (self.sid, self.work, "web-ops"))
            self.assertEqual(hub.s.project("web-ops")["match"], {"cwd": [self.work]})
            again = hub.resume_session({"session_id": self.sid[:8]})
            self.assertFalse(again["created"])
            self.assertEqual(again["agent"]["id"], a["id"])
            self.assertEqual(hub.search_sessions("lantern")["sessions"][0]["agent"], a["id"])
            # the Codex session in the same folder now joins that project instead of making another
            c = hub.resume_session({"session_id": self.cid})
            self.assertEqual((c["agent"]["provider"], c["agent"]["project"], c["created_project"]),
                             ("codex", "web-ops", False))


class ClearTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(discovery, "accounts", return_value=[{"name": "main", "kind": "claude", "model": None}])
        p.start()
        self.addCleanup(p.stop)
        self.hub = Hub(Store(os.path.join(tempfile.mkdtemp(), "hub.db")))

    def test_clear_chat_forgets_session_and_unsent_messages(self):
        self.hub.s.set_agent("desk", session_id="old-session", context_tokens=30000)
        m, _ = self.hub.s.enqueue("desk", "hello", idem="k")
        self.hub.clear_chat("desk")
        d = self.hub.s.agent("desk")
        self.assertEqual((d["session_id"], d["context_tokens"]), (None, 0))
        self.assertEqual(self.hub.s.message(m["id"])["state"], "interrupted")
        self.assertEqual(self.hub.s.messages(agent="desk", state="queued"), [])

    def test_clear_refuses_terminal_agents(self):
        self.hub.s.add_agent({"id": "t", "mode": "terminal", "provider": "claude", "session_id": "s", "state": "idle"})
        with self.assertRaises(ValueError):
            self.hub.clear_chat("t")

    def test_terminal_follows_clear_to_new_session(self):
        self.hub.s.add_agent({"id": "t", "mode": "terminal", "provider": "claude", "session_id": "s-old",
                              "state": "idle"})
        self.hub.s.add_agent({"id": "c-new", "mode": "observed", "provider": "claude", "session_id": "s-new",
                              "state": "idle"})
        with mock.patch.object(self.hub.t, "status", side_effect=lambda a: {"pid": 4242 if a["id"] == "t" else None}):
            self.hub.follow_cleared([{"pid": 4242, "sessionId": "s-new"}, {"pid": 7, "sessionId": "s-other"}])
        self.assertEqual(self.hub.s.agent("t")["session_id"], "s-new")
        self.assertTrue(self.hub.s.agent("c-new")["gone"])


if __name__ == "__main__":
    unittest.main()
