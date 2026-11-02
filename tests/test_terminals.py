"""Terminal agents: pty holder lifecycle (spawn, write, read, resize, reconnect after a hub restart), sb send typed
exactly once and confirmed from the transcript, and the account-toggle relaunch command line. Uses tests/fake_cli.py.
Run: python3 -m unittest tests.test_terminals
"""
import json
import os
import re
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
from switchboard import hub as hubmod  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402

discovery.CLAUDE_PROJECTS = os.environ["FAKE_PROJECTS"]
json.dump({"accounts": [{"name": "one", "kind": "claude", "config_dir": os.path.join(TMP, "cfg-one")},
                         {"name": "two", "kind": "claude", "config_dir": os.path.join(TMP, "cfg-two")},
                         {"name": "chatgpt", "kind": "codex", "config_dir": os.path.join(TMP, "codex")}]},
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
        self.assertEqual(t2.size, (100, 30))  # mirror at the CLI's real size, not the 120x40 default
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
        c = {"id": "y", "provider": "codex", "account": "chatgpt", "session_id": "th-1", "model": None}
        self.assertEqual(terminals.launch_argv(c)[1:3], ["resume", "th-1"])
        self.assertIn("--no-alt-screen", terminals.launch_argv(c))  # inline, so its tab scrolls
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", terminals.launch_argv(c))  # only if asked
        self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                      terminals.launch_argv({**c, "permission_mode": "bypassPermissions"}))
        self.assertTrue(terminals.launch_argv(c)[-1].startswith("developer_instructions=\""))
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


class BackgroundJobTests(unittest.TestCase):
    """An agent that finished its turn but left a shell or monitor running takes messages; exiting it needs a
    confirm (it would end those jobs). Footers as Claude Code and Codex draw them."""
    def screen(self, *lines):
        t = terminals.Term("x", {})
        t.screen.feed("\r\n".join(lines).encode())
        return t

    def test_idle_with_background_jobs(self):
        t = self.screen("✻ Baked for 21m 28s · done 10:39 AM · 1 shell, 1 monitor still running", "─" * 40, "❯ ",
                        "─" * 40, "  ⏵⏵ auto mode on · 1 shell, 1 monitor · ↓ to manage")
        self.assertEqual(t.view(), (True, False, ""))
        self.assertTrue(t.background())

    def test_codex_0166_prompt(self):
        # codex-cli 0.166 draws '» ' with a dim placeholder; an idle Codex must read as ready
        t = self.screen("  Worked for 41s • 12:30 PM", "", "» \x1b[2mAsk Codex to do anything\x1b[22m", "",
                        "  gpt-6 high · ~/project · weekly 97% left", "  ? for shortcuts")
        self.assertEqual(t.view(), (True, False, ""))
        t = self.screen("» fix the parser", "", "  ? for shortcuts")
        self.assertEqual(t.view(), (True, False, "fix the parser"))

    def test_waiting_for_background_agents_is_not_a_turn(self):
        t = self.screen("✻ Waiting for 1 background agent to finish", "─" * 40, "❯ ", "─" * 40,
                        "  ⏵⏵ auto mode on · 1 shell · ↓ to manage", "  ◯ general-purpose  Rebasing")
        self.assertEqual(t.view(), (True, False, ""))  # a message can go in; exiting would still ask first
        self.assertTrue(t.background())

    def test_running_turn_and_plain_idle(self):
        t = self.screen("✶ Pondering… (12s · ↓ 300 tokens · esc to interrupt)", "─" * 40, "❯ ", "─" * 40,
                        "  ⏵⏵ auto mode on · 1 shell · ↓ to manage")
        self.assertEqual(t.view()[:2], (True, True))
        t = self.screen("• Working (8s • esc to interrupt)", "", "› Ask Codex to do anything", "  gpt-6 medium")
        self.assertTrue(t.view()[1])
        t = self.screen("done.", "─" * 40, "❯ ", "─" * 40, "  ⏵⏵ auto mode on")
        self.assertEqual(t.view(), (True, False, ""))
        self.assertFalse(t.background())

    def test_message_typed_into_idle_agent_with_a_shell(self):
        s = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db"))
        tm = terminals.Terminals(s)
        s.add_agent({"id": "bg", "project": "p", "title": "x", "provider": "claude", "account": "one",
                     "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        t = tm.launch(s.agent("bg"))
        self.assertTrue(wait_for(lambda: t.view()[0]))
        with t.lock:  # the fake CLI cannot draw Claude's footer: add it to the mirrored screen
            t.screen.feed("\r\n  ⏵⏵ auto mode on · 1 shell · ↓ to manage".encode())
        self.assertTrue(t.background())
        with self.assertRaises(terminals.NeedsConfirm):  # stopping would end the shell
            tm.stop("bg")
        s.enqueue("bg", "hello from a peer", frm="peer")
        t.steady_for()  # the hub's tick reads this every second
        time.sleep(2)  # past the 1.5 s settle guard
        self.assertTrue(t.background())  # still showing the shell when the message goes in
        tm._deliver_one(s.agent("bg"))
        self.assertTrue(wait_for(lambda: "[from peer via sb] hello from a peer" in t.text()))
        tm.exit_cli("bg")


class RelaunchTests(unittest.TestCase):
    def test_relaunch_running_agent_with_model(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": "rl", "project": "p", "title": "x", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        t = h.t.launch(h.s.agent("rl"))
        self.assertTrue(wait_for(lambda: t.view()[0]))
        t.write("hello\r")
        self.assertTrue(wait_for(lambda: "ok: hello" in t.text() and not t.view()[1]))
        sid = h.s.agent("rl")["session_id"]
        t.write("unsent")
        self.assertTrue(wait_for(lambda: t.view()[2] == "unsent"))
        with self.assertRaises(terminals.NeedsConfirm):
            h.relaunch("rl", {"model": "claude-sonnet-5-5"})
        h.relaunch("rl", {"model": "claude-sonnet-5-5", "confirm": True})
        self.assertTrue(wait_for(lambda: "claude-sonnet-5-5" in launches()[-1]["argv"], 20))
        argv = launches()[-1]["argv"]
        self.assertEqual(argv[1:5], ["--resume", sid, "--model", "claude-sonnet-5-5"])
        self.assertEqual(h.s.agent("rl")["model"], "claude-sonnet-5-5")
        self.assertTrue(wait_for(lambda: h.t.running("rl") and not h.t.switching.get("rl")))
        h.t.exit_cli("rl")
        h.relaunch("rl", {"model": "claude-opus-5-5"})  # a stopped terminal just starts with it
        self.assertTrue(wait_for(lambda: "claude-opus-5-5" in launches()[-1]["argv"]))
        h.t.exit_cli("rl")

    def test_codex_model_and_other_modes(self):
        c = {"id": "y", "provider": "codex", "account": "chatgpt", "session_id": None, "model": "gpt-6.2"}
        self.assertEqual(terminals.launch_argv(c)[1:3], ["--model", "gpt-6.2"])
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": "mg", "project": "p", "title": "x", "provider": "codex", "account": "chatgpt",
                       "mode": "managed", "session_id": None, "cwd": TMP, "state": "idle"})
        self.assertEqual(h.relaunch("mg", {"model": "gpt-6.2", "effort": "high"})["model"], "gpt-6.2")
        self.assertEqual(h.s.agent("mg")["effort"], "high")
        with self.assertRaises(ValueError):
            h.relaunch("desk", {"model": "claude-haiku-5-5"})
        with self.assertRaises(LookupError):
            h.relaunch("nobody", {})


class SubagentTests(unittest.TestCase):
    def test_who_spawned_a_subagent(self):
        def sub(sid, aid, alias, model, desc):
            d = os.path.join(os.environ["FAKE_PROJECTS"], "-sub", sid, "subagents")
            os.makedirs(d, exist_ok=True)
            rows = [{"type": "user", "sessionId": sid, "agentId": aid, "isSidechain": True, "cwd": "/w",
                     "timestamp": "2026-11-01T15:14:03.512Z", "message": {"role": "user", "content": "go"}},
                    {"type": "assistant", "sessionId": sid, "timestamp": "2026-11-01T15:14:05.000Z",
                     "message": {"model": model, "content": [{"type": "text", "text": "ok"}]}}]
            with open(os.path.join(d, f"agent-{aid}.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")
            json.dump({"agentType": "general-purpose", "description": desc, "model": alias, "finished": True},
                      open(os.path.join(d, f"agent-{aid}.meta.json"), "w"))

        sub("sid-parent", "a1", "haiku", "claude-haiku-5-5", "Lit review")
        time.sleep(0.02)
        sub("sid-other", "a2", None, "claude-opus-5-5", "Audit pins")  # model inherited: no alias in meta
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": "research", "project": "neuro", "title": "R", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": "sid-parent", "cwd": TMP, "state": "idle"})
        got = h.subagents(model="haiku")["subagents"]
        self.assertEqual([(x["id"], x["agent"], x["project"], x["model"], x["description"]) for x in got],
                         [("a1", "research", "neuro", "claude-haiku-5-5", "Lit review")])
        everything = h.subagents()["subagents"]
        self.assertEqual([x["id"] for x in everything[:2]], ["a2", "a1"])  # newest first
        self.assertEqual(everything[0]["model"], "claude-opus-5-5")
        self.assertEqual([x["id"] for x in h.subagents(agent="research")["subagents"]], ["a1"])


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

    def test_paused_project_holds_typed_messages(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.upsert_project({"id": "held", "name": "Held", "paused": True})
        h.s.add_agent({"id": "hp", "project": "held", "title": "w", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        mid = h.send({"to": "hp", "body": "after the pause", "idem": "k-held"})["message"]["id"]
        try:
            for _ in range(3):
                h.t.tick()
            self.assertFalse(h.t.running("hp"))  # not started, nothing typed
            self.assertEqual(h.s.message(mid)["state"], "queued")
            h.s.upsert_project({"id": "held", "paused": False})
            self.assertTrue(wait_for(lambda: (h.t.tick(), h.s.message(mid)["state"])[1] == "done", 30, 0.3))
            self.assertEqual(transcript_users(h.s.agent("hp")["session_id"]), ["after the pause"])
        finally:
            if h.t.running("hp"):
                h.t.exit_cli("hp")


class QueueTests(unittest.TestCase):
    """A queue drains on what the CLI shows now: not old words in the scrollback, not a footer that keeps
    repainting; a real choice holds it (and is never answered for the owner), and the view says why it waits."""
    def hub_with(self, aid, env=None):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": aid, "project": "q", "title": aid, "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        old = {k: os.environ.get(k) for k in (env or {})}
        os.environ.update(env or {})
        try:
            h.t.launch(h.s.agent(aid))
        finally:
            for k, v in old.items():
                os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(lambda: h.t.running(aid) and h.t.exit_cli(aid))
        return h

    def deliver(self, h, aid, body, key, seconds=30):
        mid = h.send({"to": aid, "body": body, "idem": key})["message"]["id"]
        wait_for(lambda: (h.t.tick(), h.s.message(mid)["state"])[1] == "done", seconds, 0.3)
        return h.s.message(mid)

    def test_old_words_in_the_scrollback_do_not_hold_the_queue(self):
        h = self.hub_with("hist")
        self.assertEqual(self.deliver(h, "hist", "do you trust this folder? one you trust", "k1")["state"], "done")
        self.assertIn("trust this folder", h.t.term("hist").text())  # now part of the history on screen
        self.assertEqual(self.deliver(h, "hist", "next thing", "k2")["state"], "done")

    def test_a_real_choice_holds_the_queue_until_the_owner_answers(self):
        h = self.hub_with("menu", {"FAKE_TRUST_MENU": "1"})
        t = h.t.term("menu")
        self.assertTrue(wait_for(lambda: "trust this folder" in t.text()))
        mid = h.send({"to": "menu", "body": "after the choice", "idem": "k3"})["message"]["id"]
        for _ in range(4):
            h.t.tick()
            time.sleep(0.6)
        m = h.s.message(mid)
        self.assertEqual(m["state"], "queued")
        self.assertIn("asks whether to trust its folder", m["error"])
        self.assertEqual(h.agent_view(h.s.agent("menu"))["queue_reason"],
                         "the CLI asks whether to trust its folder; answer it in the terminal")
        t.write("\r")  # the owner answers in the terminal
        self.assertTrue(wait_for(lambda: (h.t.tick(), h.s.message(mid)["state"])[1] == "done", 30, 0.3))
        self.assertEqual(transcript_users(h.s.agent("menu")["session_id"]), ["after the choice"])

    def test_a_constantly_repainting_idle_screen_still_takes_messages(self):
        h = self.hub_with("clock", {"FAKE_REPAINT_S": "0.3"})
        m = self.deliver(h, "clock", "while the clock ticks", "k4")
        self.assertEqual((m["state"], m["reply"]), ("done", "ok: while the clock ticks"))

    def test_view_says_why_and_since_when(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": "zz", "project": "q", "title": "zz", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle", "paused": 1})
        h.send({"to": "zz", "body": "held", "idem": "k5"})
        h.t.tick()
        v = h.agent_view(h.s.agent("zz"))
        self.assertEqual((v["queued"], v["paused"]), (1, True))
        self.assertIn("paused", v["queue_reason"])
        self.assertTrue(v["queued_since"])


class BatchTests(unittest.TestCase):
    """Everything waiting for an agent goes in as one attributed batch backed by an owner-only file; an agent busy
    past the threshold gets one Esc first; each message is answered exactly once."""
    hub_with = QueueTests.hub_with

    def test_waiting_messages_go_in_as_one_batch(self):
        h = self.hub_with("bat")
        t = h.t.term("bat")
        self.assertTrue(wait_for(lambda: t.view()[0]))
        ids = [h.send({"to": "bat", "body": f"item {i}: lorem ipsum", "from": f"peer{i}", "idem": f"b{i}"})["message"]["id"]
               for i in range(3)]
        self.assertTrue(wait_for(lambda: (h.t.tick(), all(h.s.message(i)["state"] == "done" for i in ids))[1], 30, 0.3))
        users = transcript_users(h.s.agent("bat")["session_id"])
        self.assertEqual(len(users), 1)  # typed once, as one batch
        self.assertTrue(users[0].startswith("[sb batch batch-"))
        for i in range(3):
            self.assertIn(f"from peer{i} ({ids[i]})", users[0])
        replies = {h.s.message(i)["reply"] for i in ids}
        self.assertEqual(len(replies), 1)  # one reply answers each message once
        path = re.search(r"in full in (\S+)\]", users[0]).group(1)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        text = open(path).read()
        for i in range(3):
            self.assertIn(ids[i], text)
            self.assertIn(f"item {i}: lorem ipsum", text)

    def test_no_second_esc_after_a_hub_restart(self):
        s = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db"))
        s.add_agent({"id": "w", "project": "q", "title": "w", "provider": "claude", "account": "one",
                     "mode": "terminal", "session_id": None, "cwd": TMP, "state": "busy"})
        mid = s.enqueue("w", "old news", frm="peer")[0]["id"]
        s.x("UPDATE messages SET created_at=? WHERE id=?", ("2026-01-01T00:00:00Z", mid))

        class Busy:
            writes = []

            def dialog(self):
                return None

            def write(self, data):
                self.writes.append(data)

        first = terminals.Terminals(s)
        first._maybe_interrupt(s.agent("w"), Busy())
        self.assertEqual(Busy.writes, ["\x1b"])
        restarted = terminals.Terminals(s)  # a new hub process: nothing in memory
        restarted._maybe_interrupt(s.agent("w"), Busy())
        self.assertEqual(Busy.writes, ["\x1b"])  # still one

    def test_a_long_busy_agent_gets_one_esc_then_the_batch(self):
        old = terminals.INTERRUPT_AFTER_S
        terminals.INTERRUPT_AFTER_S = 2
        try:
            h = self.hub_with("busy", {"FAKE_BUSY_S": "120"})
            t = h.t.term("busy")
            self.assertTrue(wait_for(lambda: t.view()[0]))
            first = h.send({"to": "busy", "body": "start the long job", "idem": "j0"})["message"]["id"]
            self.assertTrue(wait_for(lambda: (h.t.tick(), t.view()[1])[1], 20, 0.3))  # now in a long turn
            ids = [h.send({"to": "busy", "body": f"while you work {i}", "from": "peer", "idem": f"j{i}"})["message"]["id"]
                   for i in (1, 2)]
            # typed and seen in the transcript (the fake takes 120 s to answer anything, the batch included)
            self.assertTrue(wait_for(lambda: (h.t.tick(), all(h.s.message(i)["state"] in ("delivered", "done")
                                                              for i in ids))[1], 40, 0.3))
            users = transcript_users(h.s.agent("busy")["session_id"])
            self.assertEqual(users[0], "start the long job")
            self.assertIn("interrupted so these could reach you", users[1])
            esc = [e for e in h.s.q("SELECT text FROM events WHERE agent=?", ("busy",)) if "interrupted:" in e["text"]]
            self.assertEqual(len(esc), 1)  # one Esc, no storm
            self.assertIn(h.s.message(first)["state"], ("delivered", "done"))
        finally:
            terminals.INTERRUPT_AFTER_S = old


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


class SlowReaderTests(unittest.TestCase):
    def test_stalled_holder_client_is_dropped_and_the_hub_keeps_up(self):
        import socket
        os.environ["SB_SHELL_BIN"] = "/bin/bash"
        s = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db"))
        tm = terminals.Terminals(s)
        s.add_agent({"id": "fl", "project": "p", "title": "x", "provider": "shell", "account": None,
                     "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        t = tm.launch(s.agent("fl"))
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled.connect(tm.paths("fl")["sock"])  # connects and never reads
        try:
            t.write("head -c 6000000 /dev/zero | tr '\\0' a; echo; echo flood-$((40+2))\r")
            # the hub may itself be dropped and reattach during the flood: always read its current mirror
            self.assertTrue(wait_for(lambda: "flood-42" in tm.term("fl").text(), 40))
            stalled.settimeout(5)
            got = 0
            while True:
                chunk = stalled.recv(1 << 20)
                if not chunk:
                    break  # the holder hung up on it
                got += len(chunk)
            self.assertLess(got, 6_000_000)
            # the hub restarts: a fresh manager reattaches to the same holder and gets the scrollback
            t = tm.term("fl")  # the current mirror, after any reattach during the flood
            holder_pid = t.state["holder_pid"]
            t.sock.shutdown(socket.SHUT_RDWR)
            self.assertTrue(wait_for(lambda: not t.alive))
            tm = terminals.Terminals(s)
            tm.reattach_all()
            t2 = tm.term("fl")
            self.assertEqual(t2.state["holder_pid"], holder_pid)
            self.assertTrue(wait_for(lambda: b"flood-42" in bytes(t2.buf)))
            t2.write("echo still-$((1+1))\r")
            self.assertTrue(wait_for(lambda: "still-2" in t2.text()))
        finally:
            stalled.close()
            tm.exit_cli("fl")

    class Browser:
        """A page whose socket can stall: send blocks while `stall` is set, until the hub aborts the connection."""
        def __init__(self, stall_on=None):
            import threading
            self.closed, self.aborts, self.sent = False, 0, []
            self.stall_on, self.unblock, self.sending_since = stall_on, threading.Event(), 0.0

        def send(self, data, opcode=None):
            if self.closed:
                raise ConnectionError("closed")
            self.sending_since = time.time()
            try:
                if self.stall_on is not None and self.stall_on(data):
                    self.unblock.wait()  # stuck in send; only the hub's abort (a socket shutdown) gets it out
                if self.closed:
                    raise ConnectionError("aborted")
                self.sent.append(data)
            finally:
                self.sending_since = 0.0

        def recv(self):
            while not self.closed:
                time.sleep(0.02)
            raise ConnectionError("closed")

        def abort(self):
            self.aborts += 1
            self.closed = True
            self.unblock.set()

        def close(self):
            self.abort()

    def session_for(self, t, b):
        import threading
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": t.aid, "project": "p", "title": "x", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        h.t.terms[t.aid] = t
        session = threading.Thread(target=h.terminal_session, args=(b, h.s.agent(t.aid), False), daemon=True)
        session.start()
        return session

    def test_browser_that_falls_behind_is_closed_not_skipped(self):
        t = terminals.Term("br", {"started_at": time.time()})
        t.buf += b"earlier output"
        b = self.Browser(stall_on=lambda d: d == b"slow")
        session = self.session_for(t, b)
        self.assertTrue(wait_for(lambda: t.listeners and len(b.sent) == 2))  # scrollback + status
        fn = next(iter(t.listeners))
        fn("o", b"slow")
        for _ in range(5000):
            fn("o", b"more")
        session.join(10)  # nobody releases the stalled send: the hub's own watchdog aborts it
        self.assertFalse(session.is_alive())
        self.assertEqual(b.aborts, 1)
        self.assertEqual(b.sent[0], b"earlier output")
        self.assertNotIn(b"more", b.sent)
        self.assertEqual(t.listeners, set())

    def test_a_browser_stuck_in_its_first_replay_is_cut_off(self):
        old = hubmod.SEND_TIMEOUT_S
        hubmod.SEND_TIMEOUT_S = 1
        try:
            t = terminals.Term("first", {"started_at": time.time()})
            t.buf += b"x" * 1000
            b = self.Browser(stall_on=lambda d: True)  # never takes even the replay
            session = self.session_for(t, b)
            session.join(10)
            self.assertFalse(session.is_alive())
            self.assertEqual((b.aborts, b.sent), (1, []))
            self.assertEqual(t.listeners, set())  # nothing left registered
        finally:
            hubmod.SEND_TIMEOUT_S = old

    def test_a_live_holder_is_never_reported_exited(self):
        import socket
        os.environ["SB_SHELL_BIN"] = "/bin/bash"
        old = terminals.REATTACH_FIRST_S
        terminals.REATTACH_FIRST_S = 3
        s = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db"))
        tm = terminals.Terminals(s)
        s.add_agent({"id": "rc", "project": "p", "title": "x", "provider": "shell", "account": None,
                     "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        tm.launch(s.agent("rc"))
        try:
            for i in range(4):  # the holder drops us four times in a row
                t = tm.term("rc")
                t.sock.shutdown(socket.SHUT_RDWR)
                if i < 3:
                    self.assertTrue(wait_for(lambda: tm.term("rc") is not t and tm.running("rc")), i)
            self.assertTrue(wait_for(lambda: tm.status(s.agent("rc"))["state"] == "reconnecting", 2))  # not "exited"
            self.assertTrue(wait_for(lambda: tm.term("rc") is not t and tm.running("rc"), 10))  # backoff got it back
            tm.term("rc").write("echo back-$((2+2))\r")
            self.assertTrue(wait_for(lambda: "back-4" in tm.term("rc").text()))
            self.assertFalse([e for e in s.q("SELECT text FROM events WHERE agent='rc'") if "exited" in e["text"]])
        finally:
            terminals.REATTACH_FIRST_S = old
            tm.exit_cli("rc")

    def test_a_reattach_resyncs_open_browsers(self):
        t = terminals.Term("rs", {"started_at": time.time()})
        t.buf += b"before"
        b = self.Browser()
        session = self.session_for(t, b)
        self.assertTrue(wait_for(lambda: t.listeners and len(b.sent) == 2))
        next(iter(t.listeners))("resync", None)  # what a reattached reader sends once the holder's replay is in
        session.join(10)
        self.assertFalse(session.is_alive())
        self.assertEqual(b.aborts, 1)  # the page reconnects and gets the whole scrollback, no gap, no repeat

    def test_a_browser_that_reconnects_before_the_replay_arrives_is_resynced(self):
        import socket
        import threading
        os.environ["SB_SHELL_BIN"] = "/bin/bash"
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        h.s.add_agent({"id": "rp", "project": "p", "title": "x", "provider": "shell", "account": None,
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        h.t.launch(h.s.agent("rp")).write("echo mark-$((1+1))\r")
        self.assertTrue(wait_for(lambda: b"mark-2" in bytes(h.t.term("rp").buf)))

        def page():
            b = self.Browser()
            threading.Thread(target=h.terminal_session, args=(b, h.s.agent("rp"), False), daemon=True).start()
            self.assertTrue(wait_for(lambda: b.sent))  # its replay (if any) and status are out
            return b

        first = page()
        gate, reader = threading.Event(), h.t._reader
        h.t._reader = lambda t: (gate.wait(10) if getattr(t, "quiet", False) else None, reader(t))
        try:
            old = h.t.term("rp")
            old.sock.shutdown(socket.SHUT_RDWR)  # the holder drops us; its replay to our new socket is late
            self.assertTrue(wait_for(lambda: h.t.terms["rp"] is not old))
            early = page()  # a page that connects in the gap reads the new, still empty buffer
            self.assertNotIn(b"mark-2", b"".join(x for x in early.sent if isinstance(x, bytes)))
            time.sleep(2.5)  # a told page is aborted within about a second
            self.assertEqual((first.aborts, early.aborts), (0, 0))  # nobody is told to reconnect before the replay
        finally:
            gate.set()
        try:
            # once the replay is in, both are told to reconnect, and a page connecting now gets all of it
            self.assertTrue(wait_for(lambda: first.aborts == 1 and early.aborts == 1))
            late = page()
            self.assertIn(b"mark-2", b"".join(x for x in late.sent if isinstance(x, bytes)))
        finally:
            h.t.exit_cli("rp")


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
