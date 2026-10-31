"""Terminal agents: pty holder lifecycle (spawn, write, read, resize, reconnect after a hub restart), sb send typed
exactly once and confirmed from the transcript, and the account-toggle relaunch command line. Uses tests/fake_cli.py.
Run: python3 -m unittest tests.test_terminals
"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbterm-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_WORKSPACE"] = os.path.join(TMP, "ws")
os.environ["SB_CONFIG"] = os.path.join(TMP, "accounts.json")
os.environ["SB_CLAUDE_BIN"] = os.path.join(HERE, "fake_cli.py")
os.environ["SB_CODEX_BIN"] = os.path.join(HERE, "fake_cli.py")
os.environ["FAKE_PROJECTS"] = os.path.join(TMP, "projects")
os.environ["FAKE_CLI_LOG"] = os.path.join(TMP, "cli.jsonl")
os.environ["FAKE_BUSY_S"] = "0.5"

from switchboard import discovery, terminals  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402

discovery.CLAUDE_PROJECTS = os.environ["FAKE_PROJECTS"]
json.dump({"accounts": [{"name": "one", "kind": "claude", "config_dir": os.path.join(TMP, "cfg-one")},
                         {"name": "two", "kind": "claude", "config_dir": os.path.join(TMP, "cfg-two")},
                         {"name": "oai", "kind": "codex", "config_dir": os.path.join(TMP, "codex")}]},
          open(os.environ["SB_CONFIG"], "w"))


def wait_for(fn, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def launches():
    try:
        return [json.loads(l) for l in open(os.environ["FAKE_CLI_LOG"])]
    except OSError:
        return []


def transcript_users(sid):
    p = os.path.join(os.environ["FAKE_PROJECTS"], "fake", sid + ".jsonl")
    try:
        rows = [json.loads(l) for l in open(p)]
    except OSError:
        return []
    return [r["message"]["content"][0]["text"] for r in rows if r["type"] == "user"]


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db"))
        self.t = terminals.Terminals(self.s)
        self.aid = "t" + str(int(time.time() * 1000) % 10_000_000)
        self.s.add_agent({"id": self.aid, "project": "p", "title": "x", "provider": "claude", "account": "one",
                          "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})

    def tearDown(self):
        if self.t.running(self.aid):
            self.t.exit_cli(self.aid)

    def test_spawn_write_read_resize_reconnect(self):
        t = self.t.launch(self.s.agent(self.aid))
        self.assertTrue(wait_for(lambda: "FAKE CLI" in t.text()))
        sid = self.s.agent(self.aid)["session_id"]
        self.assertIn("--session-id", launches()[-1]["argv"])
        self.assertEqual(launches()[-1]["SB_AGENT"], self.aid)
        self.assertEqual(launches()[-1]["CLAUDE_CONFIG_DIR"], os.path.join(TMP, "cfg-one"))
        t.write("hello there\r")
        self.assertTrue(wait_for(lambda: "ok: hello there" in t.text()))
        self.assertTrue(wait_for(lambda: t.view()[0] and not t.view()[1]))
        t.resize(100, 30)
        self.assertTrue(wait_for(lambda: "size 100x30" in t.text()))
        holder_pid = t.state["holder_pid"]

        # "hub restart": drop this manager without touching the holder, reattach from a fresh one
        import socket
        t.sock.shutdown(socket.SHUT_RDWR)
        self.assertTrue(wait_for(lambda: not t.alive))
        self.assertTrue(discovery.pid_alive(holder_pid))
        t2m = terminals.Terminals(self.s)
        t2m.reattach_all()
        self.t = t2m
        t2 = t2m.term(self.aid)
        self.assertIsNotNone(t2)
        self.assertTrue(wait_for(lambda: b"ok: hello there" in bytes(t2.buf)))  # scrollback replayed
        t2.write("again\r")
        self.assertTrue(wait_for(lambda: "ok: again" in t2.text()))
        self.assertEqual(transcript_users(sid), ["hello there", "again"])

        self.assertTrue(t2m.exit_cli(self.aid))
        self.assertFalse(t2m.running(self.aid))
        self.assertTrue(wait_for(lambda: not discovery.pid_alive(holder_pid)))
        self.assertIn(b"ok: again", t2m.scrollback(self.aid))  # kept on disk for a later browser

    def test_relaunch_resumes_and_keys_reach_cli(self):
        t = self.t.launch(self.s.agent(self.aid))
        self.assertTrue(wait_for(lambda: t.view()[0]))
        t.write("draft")
        self.assertTrue(wait_for(lambda: t.view()[2] == "draft"))
        t.write("\x03")  # Ctrl+C clears the composer
        self.assertTrue(wait_for(lambda: t.view()[2] == ""))
        t.write("first\r")
        self.assertTrue(wait_for(lambda: "ok: first" in t.text()))
        sid = self.s.agent(self.aid)["session_id"]
        self.t.exit_cli(self.aid)
        n = len(launches())
        t = self.t.launch(self.s.agent(self.aid))
        # the old scrollback is replayed at once, so wait for the new CLI's own launch record
        self.assertTrue(wait_for(lambda: len(launches()) > n))
        self.assertEqual(launches()[-1]["argv"][1:3], ["--resume", sid])


class AccountToggleTests(unittest.TestCase):
    def test_relaunch_command_line(self):
        a = {"id": "x", "provider": "claude", "account": "two", "session_id": "sid-1", "model": "claude-opus-5-5",
             "permission_mode": "auto", "effort": None}
        old = terminals.has_history
        terminals.has_history = lambda agent: True
        try:
            argv = terminals.launch_argv(a)
        finally:
            terminals.has_history = old
        self.assertEqual(argv[1:7], ["--resume", "sid-1", "--model", "claude-opus-5-5", "--permission-mode", "auto"])
        self.assertEqual(argv[7], "--append-system-prompt")
        self.assertIn("sb context", argv[8])
        env = terminals.launch_env(a)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(TMP, "cfg-two"))
        self.assertEqual(env["SB_AGENT"], "x")
        c = {"id": "y", "provider": "codex", "account": "oai", "session_id": "th-1", "model": None}
        self.assertEqual(terminals.launch_argv(c)[1:3], ["resume", "th-1"])
        self.assertTrue(terminals.launch_argv(c)[4].startswith("developer_instructions=\""))
        self.assertEqual(terminals.launch_env(c)["CODEX_HOME"], os.path.join(TMP, "codex"))

    def test_switch_running_agent(self):
        s = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db"))
        tm = terminals.Terminals(s)
        s.add_agent({"id": "sw", "project": "p", "title": "x", "provider": "claude", "account": "one",
                     "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        t = tm.launch(s.agent("sw"))
        self.assertTrue(wait_for(lambda: t.view()[0]))
        t.write("before switch\r")
        self.assertTrue(wait_for(lambda: "ok: before switch" in t.text() and not t.view()[1]))
        sid = s.agent("sw")["session_id"]
        t.write("unsent")
        self.assertTrue(wait_for(lambda: t.view()[2] == "unsent"))
        with self.assertRaises(terminals.NeedsConfirm):  # unsent text counts as busy
            tm.switch_account("sw", "two")
        tm.switch_account("sw", "two", confirm=True)
        self.assertTrue(wait_for(lambda: launches()[-1]["CLAUDE_CONFIG_DIR"] == os.path.join(TMP, "cfg-two"), 20))
        last = launches()[-1]
        self.assertEqual(last["argv"][1:3], ["--resume", sid])
        self.assertEqual(s.agent("sw")["account"], "two")
        self.assertTrue(wait_for(lambda: tm.running("sw") and not tm.switching.get("sw")))
        tm.exit_cli("sw")


class SendTests(unittest.TestCase):
    def test_send_typed_exactly_once_and_confirmed(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.upsert_project({"id": "proj", "name": "Proj"})
        h.s.add_agent({"id": "lead1", "project": "proj", "title": "lead", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        h.s.add_agent({"id": "other", "project": "proj", "title": "other", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        with self.assertRaises(ValueError):  # two agents, no lead set
            h.send({"to": "proj", "body": "x"})
        h.s.upsert_project({"id": "proj", "lead": "lead1"})
        r1 = h.send({"to": "proj", "body": "please do the thing", "idem": "k-1"})
        r2 = h.send({"to": "proj", "body": "please do the thing", "idem": "k-1"})
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r1["message"]["id"], r2["message"]["id"])
        self.assertEqual(r1["message"]["to"], "lead1")
        mid = r1["message"]["id"]
        try:
            # launches lazily, waits for an empty idle composer, types once, confirms from the transcript
            done = wait_for(lambda: (h.t.tick(), h.s.message(mid)["state"])[1] == "done", 30, 0.3)
            self.assertTrue(done, h.s.message(mid))
            for _ in range(5):
                h.t.tick()
            sid = h.s.agent("lead1")["session_id"]
            self.assertEqual(transcript_users(sid), ["please do the thing"])
            self.assertEqual(h.s.message(mid)["reply"], "ok: please do the thing")
            self.assertFalse(h.t.running("other"))  # nobody else got it or was started
        finally:
            for aid in ("lead1", "other"):
                if h.t.running(aid):
                    h.t.exit_cli(aid)

    def test_busy_composer_waits(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": "w1", "project": "p", "title": "w", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        t = h.t.launch(h.s.agent("w1"))
        try:
            self.assertTrue(wait_for(lambda: t.view()[0]))
            t.write("owner typing")
            self.assertTrue(wait_for(lambda: t.view()[2] == "owner typing"))
            mid = h.send({"to": "w1", "body": "from sb", "idem": "k-2"})["message"]["id"]
            for _ in range(8):
                h.t.tick()
                time.sleep(0.3)
            self.assertEqual(h.s.message(mid)["state"], "queued")  # never typed over the owner's text
            t.write("\x03")
            self.assertTrue(wait_for(lambda: (h.t.tick(), h.s.message(mid)["state"])[1] == "done", 20, 0.3))
            self.assertEqual(transcript_users(h.s.agent("w1")["session_id"]), ["from sb"])
        finally:
            h.t.exit_cli("w1")


class ShellTests(unittest.TestCase):
    def test_plain_shell_terminal(self):
        os.environ["SB_SHELL_BIN"] = "/bin/bash"
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        a = h.create_agent({"provider": "shell", "project": "p", "cwd": TMP})
        self.assertEqual((a["account"], a["session_id"], a["model"]), (None, None, None))
        with self.assertRaises(ValueError):  # shells never get typed messages
            h.send({"to": a["id"], "body": "hi"})
        t = h.t.launch(h.s.agent(a["id"]))
        try:
            t.write("echo sb-$((40+2))\r")
            self.assertTrue(wait_for(lambda: "sb-42" in t.text()))
            self.assertEqual(h.t.status(h.s.agent(a["id"]))["state"], "idle")
            self.assertNotIn(a["id"], h.router_summary())
        finally:
            self.assertTrue(h.t.exit_cli(a["id"]))


class ModelFollowTests(unittest.TestCase):
    def test_model_switched_in_the_tui_shows_up(self):
        root = os.path.join(TMP, "projects", "m")
        os.makedirs(root, exist_ok=True)
        rows = [{"type": "assistant", "timestamp": "2026-10-30T20:00:00.000Z", "message": {"model": "claude-sonnet-5-5",
                 "content": [{"type": "text", "text": "a"}]}},
                {"type": "assistant", "timestamp": "2026-10-30T20:01:00.000Z", "message": {"model": "claude-opus-5-5",
                 "content": [{"type": "text", "text": "b"}]}},
                {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-30T20:02:00.000Z",
                 "message": {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": "sub"}]}},
                {"type": "assistant", "timestamp": "2026-10-30T20:03:00.000Z", "message": {"model": "<synthetic>",
                 "content": [{"type": "text", "text": "err"}]}}]
        with open(os.path.join(root, "sid-model.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": "mf", "project": "p", "title": "m", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": "sid-model", "cwd": TMP, "state": "idle",
                       "model": "claude-sonnet-5-5"})
        h.refresh_replies()
        self.assertEqual(h.s.agent("mf")["model"], "claude-opus-5-5")


class NotificationTests(unittest.TestCase):
    def test_notification_sources(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.upsert_project({"id": "ops", "name": "Ops", "match": {"cwd": [TMP]}})
        h.s.add_agent({"id": "w", "project": "ops", "title": "w", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        n1 = h.notify("w", "need you", idem="k")
        n2 = h.notify("w", "need you again", idem="k")
        self.assertEqual(n1["id"], n2["id"])
        self.assertEqual((n1["project"], n1["level"]), ("ops", "need"))
        h.ack(n1["id"])
        self.assertEqual(h.notifications()["items"], [])
        # a decision (sb ask) pages the owner; answering clears it
        h.s.x("INSERT INTO decisions(id,project,agent,question,options,created_at) VALUES('d1','ops','w','ship?','[]','x')")
        h.notify("w", "asks: ship?", kind="decision", idem="decision:d1")
        self.assertEqual(h.notifications()["need"], 1)
        h.answer("d1", "yes")
        self.assertEqual(h.notifications()["need"], 0)
        # a terminal stuck on a prompt for the owner pages the owner once, and clears when the prompt is gone
        t = h.t.launch(h.s.agent("w"))
        try:
            self.assertTrue(wait_for(lambda: t.view()[0]))
            with t.lock:
                t.screen.feed("\r\n Do you want to proceed?\r\n ❯ 1. Yes\r\n Enter to confirm · Esc to cancel\r\n")
                t.last_output = 0
            h.t.tick()
            h.t.tick()
            items = h.notifications()["items"]
            self.assertEqual([(i["agent"], i["kind"]) for i in items], [("w", "waiting")])
            self.assertIn("permission", items[0]["text"])
            with t.lock:
                t.screen.feed("\x1b[2J\x1b[H\x1b[1;32m❯\x1b[0m ")
            h.t.tick()
            self.assertEqual(h.notifications()["items"], [])
        finally:
            h.t.exit_cli("w")
        # dispatch leaves an FYI; focus requests are exposed to the page
        r = h.dispatch({"project": "ops", "task": "fix it", "idem": "task-9"})
        info = [i for i in h.notifications()["items"] if i["kind"] == "dispatch"]
        self.assertEqual((len(info), info[0]["level"], info[0]["agent"]), (1, "info", r["agent"]["id"]))
        h.set_focus(r["agent"]["id"])
        self.assertEqual(h.overview()["focus"]["agent"], r["agent"]["id"])


class DispatchTests(unittest.TestCase):
    def test_dispatch_once_and_task_typed_once(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.upsert_project({"id": "ops", "name": "Ops", "match": {"cwd": [TMP]}})
        r1 = h.dispatch({"project": "ops", "task": "fix bug 42", "idem": "task-1", "from": "desk"})
        r2 = h.dispatch({"project": "ops", "task": "fix bug 42", "idem": "task-1", "from": "desk"})
        a = r1["agent"]
        self.assertEqual((r2["duplicate"], r2["agent"]["id"]), (True, a["id"]))
        self.assertEqual((a["model"], a["mode"], a["cwd"], a["project"]), (None, "terminal", TMP, "ops"))
        self.assertEqual(len([x for x in h.s.agents() if x["project"] == "ops"]), 1)
        try:
            mid = h.s.one("SELECT id FROM messages WHERE idem='task-1'")["id"]
            self.assertTrue(wait_for(lambda: (h.t.tick(), h.s.message(mid)["state"])[1] == "done", 30, 0.3))
            self.assertEqual(transcript_users(h.s.agent(a["id"])["session_id"]),
                             ["[from desk via sb] fix bug 42"])
            self.assertIn("--permission-mode", launches()[-1]["argv"])
        finally:
            h.t.exit_cli(a["id"])


class RouterTests(unittest.TestCase):
    def test_router_is_small_fast_and_routes(self):
        from switchboard import runner
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        d = h.s.agent("desk")
        self.assertEqual((d["mode"], d["role"], d["model"]), ("managed", "desk", "claude-sonnet-5-5"))
        h.s.upsert_project({"id": "proj", "name": "Proj", "next_step": "ship it"})
        h.s.add_agent({"id": "worker", "project": "proj", "title": "w", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        summary = h.router_summary()
        self.assertIn("- proj - Proj: worker (stopped, default) · ship it", summary)
        self.assertNotIn("desk", summary)
        self.assertLess(len(summary), 4000)
        d["_summary"] = summary
        argv = runner.turn_command(d, "/dev/null", True)
        note = argv[argv.index("--append-system-prompt") + 1]
        self.assertTrue(note.startswith(runner.ROUTER_NOTE))
        self.assertIn("[[open:", note)
        self.assertIn(summary, note)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--tools=Bash", argv)
        self.assertIn("--allowedTools=Bash(sb *)", argv)
        view = h.overview()
        self.assertEqual(view["router"], "desk")
        self.assertIsNone(h.lead_of("switchboard"))  # the router is never a project's lead
        with self.assertRaises(PermissionError):  # a stale browser pane must not turn the router into a terminal
            h.t.launch(h.s.agent("desk"))
        self.assertEqual(h.s.agent("desk")["mode"], "managed")


if __name__ == "__main__":
    unittest.main()
