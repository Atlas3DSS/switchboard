"""Shared project context (sb context / sb note), summoned side agents (forks), and the hub restart guard."""
import json
import os
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SB_STATE"] = tempfile.mkdtemp(prefix="sbtest-")
os.environ["SB_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="sbcfg-"), "accounts.json")
os.environ["SB_WORKSPACE"] = tempfile.mkdtemp(prefix="sbws-")

from switchboard import discovery, terminals  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"))


ACCOUNTS = [{"name": "main", "kind": "claude", "config_dir": ""},
            {"name": "chatgpt", "kind": "codex", "config_dir": ""}]


class SharedContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "claude")
        os.makedirs(os.path.join(root, "-p"))
        p = mock.patch.multiple(discovery, CLAUDE_PROJECTS=root, EXTRA_PROJECTS=[])
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(discovery, "accounts", return_value=ACCOUNTS)
        q.start()
        self.addCleanup(q.stop)
        self.hub = Hub(Store(os.path.join(self.tmp, "hub.db")))
        for pid in ("alpha", "beta"):
            self.hub.s.upsert_project({"id": pid, "name": pid})
        self.sid = {}
        for aid, pid, said in (("a1", "alpha", "the parser is fixed in src/parse.py"),
                               ("a2", "alpha", "benchmarks are at 41 tok/s"), ("b1", "beta", "secret beta plans")):
            sid = str(uuid.uuid4())
            self.sid[aid] = sid
            with open(os.path.join(root, "-p", sid + ".jsonl"), "w") as f:
                f.write(dumps({"type": "user", "timestamp": "2026-10-30T10:00:00Z",
                               "message": {"role": "user", "content": f"status of {aid}?"}}) + "\n")
                f.write(dumps({"type": "assistant", "timestamp": "2026-10-30T10:00:05Z",
                               "message": {"role": "assistant", "model": "claude-opus-5-5",
                                           "content": [{"type": "text", "text": said}]}}) + "\n")
            self.hub.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "main",
                                  "mode": "terminal", "session_id": sid, "cwd": self.tmp, "state": "idle",
                                  "model": "claude-opus-5-5"})

    def test_board_shows_project_agents_and_notes_only(self):
        self.hub.add_note({"from": "a1", "text": "parser done; do not touch src/parse.py"})
        ctx = self.hub.project_context(None, viewer="a2")
        self.assertEqual(ctx["project"], "alpha")
        self.assertEqual({a["id"] for a in ctx["agents"]}, {"a1", "a2"})
        a1 = next(a for a in ctx["agents"] if a["id"] == "a1")
        self.assertEqual((a1["asked"], a1["said"]), ("status of a1?", "the parser is fixed in src/parse.py"))
        self.assertEqual([n["text"] for n in ctx["notes"]], ["parser done; do not touch src/parse.py"])
        self.assertEqual(self.hub.project_context("beta")["notes"], [])  # the owner reads any project

    def test_agents_cannot_read_other_projects(self):
        with self.assertRaises(PermissionError):
            self.hub.agent_context("b1", viewer="a1")
        with self.assertRaises(PermissionError):
            self.hub.project_context("beta", viewer="a1")
        with self.assertRaises(PermissionError):
            self.hub.add_note({"from": "a1", "project": "beta", "text": "hi"})
        self.assertIn("secret beta plans", self.hub.agent_context("b1", viewer="owner")["text"])

    def test_digest_and_search(self):
        text = self.hub.agent_context("a1", viewer="a2")["text"]
        self.assertIn("ASKED: status of a1?", text)
        self.assertIn("SAID: the parser is fixed", text)
        hits = self.hub.context_search(None, "tok/s", viewer="a1")["hits"]
        self.assertEqual([(h["agent"], h["role"]) for h in hits], [("a2", "assistant")])
        self.assertEqual(self.hub.context_search(None, "secret", viewer="a1")["hits"], [])

    def test_summon_fork_and_fresh(self):
        r = self.hub.summon({"beside": "a1", "task": "btw why is the parser slow?", "fork": True})
        a = self.hub.s.agent(r["agent"]["id"])
        self.assertTrue(r["forked"])
        self.assertEqual((a["project"], a["provider"]), ("alpha", "claude"))
        self.assertEqual(json.loads(a["fork_from"]), {"agent": "a1", "session": self.sid["a1"]})
        argv = terminals.launch_argv(a)
        self.assertEqual(argv[1:6], ["--resume", self.sid["a1"], "--fork-session", "--session-id", a["session_id"]])
        self.assertIn("forked from agent 'a1'", argv[-1])
        q = self.hub.s.messages(agent=a["id"], state="queued")
        self.assertIn("btw why is the parser slow?", q[0]["body"])
        r2 = self.hub.summon({"beside": "a2", "task": "check the benchmark"})
        a2 = self.hub.s.agent(r2["agent"]["id"])
        self.assertFalse(r2["forked"])
        self.assertIsNone(a2["fork_from"])
        self.assertIn("sb context a2", self.hub.s.messages(agent=a2["id"], state="queued")[0]["body"])

    def test_codex_fork_command(self):
        a = {"id": "x", "provider": "codex", "session_id": None, "project": "alpha", "model": None,
             "fork_from": json.dumps({"agent": "c", "session": "th-src"})}
        argv = terminals.launch_argv(a)
        self.assertEqual(argv[1:3], ["fork", "th-src"])
        a["session_id"] = "th-new"
        self.assertEqual(terminals.launch_argv(a)[1:3], ["resume", "th-new"])

    def test_running_fork_does_not_block_its_source(self):
        fork_cmd = f"claude --resume {self.sid['a1']} --fork-session --session-id NEW-SID --append-system-prompt x"
        t = self.hub.t
        t._procs_cache = (9e12, [(4242, fork_cmd)])
        self.assertIsNone(t.launch_block(self.hub.s.agent("a1")))
        self.hub.s.add_agent({"id": "f", "project": "alpha", "provider": "claude", "mode": "terminal",
                              "session_id": "NEW-SID", "state": "idle"})
        self.assertIn("another process", t.launch_block(self.hub.s.agent("f")))


class RestartTests(unittest.TestCase):
    def test_broken_code_is_not_restarted_into(self):
        hub = Hub(Store(os.path.join(tempfile.mkdtemp(), "hub.db")))
        bad = mock.Mock(returncode=1, stderr="SyntaxError: invalid syntax", stdout="")
        with mock.patch("switchboard.hub.subprocess.run", return_value=bad), \
                mock.patch("switchboard.hub.os.execv") as ex:
            with self.assertRaises(PermissionError):
                hub.restart()
        ex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
