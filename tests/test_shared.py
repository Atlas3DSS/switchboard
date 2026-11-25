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

    @unittest.skip("stale: written for the router chat / earlier isolation rules; needs rewriting")
    def test_project_islands_messages_dispatch_replies_and_queue(self):
        h = self.hub
        h.send({"from": "a1", "to": "a2", "body": "same project"})
        h.send({"from": "owner", "to": "b1", "body": "owner still has access"})
        for frm in ("a1", "desk", "unknown"):
            with self.assertRaises(PermissionError):
                h.send({"from": frm, "to": "b1", "body": "blocked"})
        with self.assertRaises(PermissionError):
            h.dispatch({"from": "a1", "project": "beta", "task": "blocked"})
        with self.assertRaises(PermissionError):
            h.s.enqueue("b1", "direct store blocked", frm="a1")
        m, _ = h.s.enqueue("a2", "legacy cross project", frm="a1")
        h.s.x("UPDATE messages SET to_agent='b1' WHERE id=?", (m["id"],))
        h.s.hold_cross_project_messages()
        self.assertEqual(h.s.message(m["id"])["state"], "held")
        h.r.route_replies(h.s.agent("b1"), [h.s.message(m["id"])], "must not return")
        self.assertFalse(h.s.one("SELECT id FROM messages WHERE idem=?", ("reply:" + m["id"],)))
        h.s.set_agent("a1", project="unsorted")
        with self.assertRaises(PermissionError):
            h.send({"from": "a1", "to": "b1", "body": "no unsorted exception"})

    def test_read_grant_opens_reading_and_nothing_else(self):
        from switchboard import shared
        with self.assertRaises(ValueError):
            self.hub.grant_reads("alpha", ["nowhere"])
        res = self.hub.grant_reads("alpha", ["beta"])
        self.assertEqual((res["reads"], sorted(res["told"])), (["beta"], ["a1", "a2"]))
        self.assertEqual(res["grants"][0]["folders"], [os.path.realpath(self.tmp)])
        told = self.hub.s.q("SELECT * FROM messages WHERE to_agent='a1'")
        self.assertEqual(told[0]["from_agent"], "system")
        self.assertIn("READ-ONLY access to project 'beta'", told[0]["body"])
        self.assertIn("secret beta plans", self.hub.agent_context("b1", viewer="a1")["text"])
        ctx = self.hub.project_context("beta", viewer="a1")
        self.assertTrue(ctx["read_only"])
        self.assertEqual(self.hub.project_context(None, viewer="a1")["reads"], ["beta"])
        self.assertTrue(self.hub.context_search("beta", "secret plans", viewer="a1")["hits"])
        with self.assertRaises(PermissionError) as err:
            self.hub.add_note({"from": "a1", "project": "beta", "text": "hi"})
        self.assertIn("read-only", str(err.exception))
        with self.assertRaises(PermissionError):
            self.hub.send({"from": "a1", "to": "b1", "body": "hello"})
        with self.assertRaises(PermissionError):  # a grant runs one way
            self.hub.agent_context("a1", viewer="b1")
        note = shared.launch_note(self.hub.s.agent("a1"))
        self.assertIn("READ-ONLY access to project 'beta'", note)
        self.assertNotIn("READ-ONLY", shared.launch_note(self.hub.s.agent("b1")))
        self.hub.s.upsert_project({"id": "alpha", "goal": "an agent edits its own project card"})
        self.assertEqual(self.hub.s.project("alpha")["reads"], ["beta"])
        self.hub.grant_reads("alpha", [])
        with self.assertRaises(PermissionError):
            self.hub.agent_context("b1", viewer="a1")

    def test_outside_toggles_are_separate_and_reach_claude_flags(self):
        from switchboard import shared
        from switchboard.terminals import NeedsConfirm
        a1 = self.hub.s.agent("a1")
        settings = lambda a: json.loads(terminals.file_flags(a)[1])  # noqa: E731
        self.assertNotIn("permissions", settings(a1))
        self.assertNotIn("outside its own folder", shared.launch_note(a1))
        for aid in ("a1", "a2"):
            self.hub.s.set_agent(aid, provider="claude", mode="terminal")
        busy = lambda aid, upd, confirm=False: (_ for _ in ()).throw(NeedsConfirm("busy")) if aid == "a2" else None  # noqa: E731
        with mock.patch.object(self.hub.t, "running", return_value=True), \
                mock.patch.object(self.hub.t, "relaunch", side_effect=busy) as rl:
            res = self.hub.set_outside("alpha", read=True)
            self.assertEqual(rl.call_count, 2)
        self.assertEqual((res["read"], res["write"], res["relaunched"], res["next_start"]), (True, False, ["a1"], ["a2"]))
        self.assertEqual(settings(self.hub.s.agent("a1"))["permissions"]["allow"], ["Read(//**)"])
        self.assertIn("read only", shared.launch_note(self.hub.s.agent("a1")))
        told = self.hub.s.q("SELECT body FROM messages WHERE to_agent='a2' ORDER BY rowid")
        self.assertIn("next time your CLI starts", told[-1]["body"])
        with mock.patch.object(self.hub.t, "running", return_value=False):
            res = self.hub.set_outside("alpha", read=False, write=True)
            self.assertEqual(settings(self.hub.s.agent("a1"))["permissions"]["allow"], ["Edit(//**)"])
            self.hub.set_outside("alpha", read=True)
            self.assertEqual(settings(self.hub.s.agent("a1"))["permissions"]["allow"], ["Read(//**)", "Edit(//**)"])
            self.assertEqual(self.hub.s.outside("beta"), {"read": False, "write": False})
            self.assertNotIn("permissions", settings(self.hub.s.agent("b1")))
            n = len(self.hub.s.q("SELECT id FROM messages WHERE to_agent='a1'"))
            self.hub.set_outside("alpha", read=True)  # no change: nobody is told
            self.assertEqual(len(self.hub.s.q("SELECT id FROM messages WHERE to_agent='a1'")), n)
            self.hub.s.upsert_project({"id": "alpha", "goal": "a card edit keeps the toggles"})
            self.assertEqual(self.hub.s.outside("alpha"), {"read": True, "write": True})
            self.hub.set_outside("alpha", read=False, write=False)
        self.assertIn("closed files outside", self.hub.s.q("SELECT body FROM messages WHERE to_agent='a1' ORDER BY rowid")[-1]["body"])
        with self.assertRaises(LookupError):
            self.hub.set_outside("nowhere", read=True)

    def test_agent_http_scope(self):
        import threading
        import urllib.request
        import urllib.error
        from http.server import ThreadingHTTPServer
        from switchboard.hub import make_handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.hub))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Host": "127.0.0.1:8802", "Content-Type": "application/json", "X-Switchboard-Agent": "a1"})
            return json.load(urllib.request.urlopen(req))
        try:
            self.assertEqual({a["id"] for a in request("/api/agents")["agents"]}, {"a1", "a2"})
            for path, body in (("/api/agents/b1/context", None),
                               ("/api/messages", {"from": "owner", "to": "b1", "body": "spoof"}),
                               ("/api/broadcast", {"body": "global"})):
                with self.assertRaises(urllib.error.HTTPError) as err:
                    request(path, body)
                self.assertEqual(err.exception.code, 409)
            result = request("/api/messages", {"to": "a2", "body": "same island"})
            self.assertEqual(result["message"]["from"], "a1")
            with self.assertRaises(urllib.error.HTTPError):  # an agent cannot give its own project a read grant
                request("/api/projects/alpha/reads", {"reads": ["beta"]})
            self.assertEqual(self.hub.s.project("alpha")["reads"], [])
            self.hub.grant_reads("alpha", ["beta"])
            self.assertIn("secret beta plans", request("/api/agents/b1/context")["text"])
            self.assertTrue(request("/api/projects/beta/context")["read_only"])
            for path, body in (("/api/messages", {"to": "b1", "body": "still closed"}),
                               ("/api/notes", {"project": "beta", "text": "still closed"}),
                               ("/api/agents/b1/interrupt", {})):
                with self.assertRaises(urllib.error.HTTPError):
                    request(path, body)
            with self.assertRaises(urllib.error.HTTPError):  # reading opens context only, not the agent's chat or card
                request("/api/agents/b1/chat")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

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

    def test_antigravity_launch_note_and_settings(self):
        from switchboard import antigravity, shared
        cwd = tempfile.mkdtemp()
        a = {"id": "gem1", "project": "alpha", "provider": "antigravity", "cwd": cwd,
             "session_id": None, "model": "gemini-4-neon-high"}
        note = shared.launch_note(a)
        self.assertIn("You are agent 'gem1' on project 'alpha'", note)
        self.assertNotIn("auto-memory", note)
        self.assertLess(len(note), 2200)
        with mock.patch.object(antigravity, "trust", return_value=False), \
                mock.patch.object(antigravity, "model_arg", return_value="Gemini 4 Neon (High)"):
            argv = terminals.launch_argv(a)
        self.assertIn("--settings", argv)
        cfg = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(cfg["artifactReviewPolicy"], "always-proceed")
        self.assertFalse(cfg["showTips"])
        self.assertNotIn("allowNonWorkspaceAccess", cfg)
        a_sw = {**a, "project": "switchboard"}
        with mock.patch.object(antigravity, "trust", return_value=False), \
                mock.patch.object(antigravity, "model_arg", return_value="Gemini 4 Neon (High)"):
            argv_sw = terminals.launch_argv(a_sw)
        self.assertTrue(json.loads(argv_sw[argv_sw.index("--settings") + 1])["allowNonWorkspaceAccess"])


if __name__ == "__main__":
    unittest.main()
