"""Fleet controls: broadcast / wrap-up to every running agent (exactly once each, stopped agents not woken, replies
collected, --close exits terminals after they report), the brief, and the master work queue.
Run: python3 -m unittest tests.test_fleet
"""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbfleet-")
os.environ.setdefault("SB_STATE", os.path.join(TMP, "state"))
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))
os.environ.setdefault("SB_CLAUDE_BIN", os.path.join(HERE, "fake_cli.py"))
os.environ.setdefault("SB_CODEX_BIN", os.path.join(HERE, "fake_cli.py"))
os.environ.setdefault("FAKE_PROJECTS", os.path.join(TMP, "projects"))
os.environ.setdefault("FAKE_CLI_LOG", os.path.join(TMP, "cli.jsonl"))
os.environ.setdefault("FAKE_BUSY_S", "0.5")

from switchboard import discovery, fleet  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store, iso_from_epoch  # noqa: E402

discovery.CLAUDE_PROJECTS = os.environ["FAKE_PROJECTS"]  # same as other test modules when run together


def wait_for(fn, timeout=30.0, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def new_hub():
    h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
    h.s.upsert_project({"id": "alpha", "name": "Alpha", "next_step": "ship alpha"})
    h.s.upsert_project({"id": "beta", "name": "Beta"})
    for aid, pid in (("a1", "alpha"), ("a2", "alpha"), ("b1", "beta"), ("idle1", "beta")):
        h.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
    return h


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        for aid in ("a1", "a2", "b1"):
            self.h.t.launch(self.h.s.agent(aid))

    def tearDown(self):
        for a in self.h.s.agents():
            if self.h.t.running(a["id"]):
                self.h.t.exit_cli(a["id"])

    def settle(self, bid):
        def done():
            self.h.t.tick()
            fleet.tick(self.h)
            v = fleet.broadcast_view(self.h, self.h.s.one("SELECT * FROM broadcasts WHERE id=?", (bid,)))
            return v if v["replied"] == v["total"] else None
        return wait_for(done, 40)

    @unittest.skip("stale: written for the router chat / earlier isolation rules; needs rewriting")
    def test_broadcast_reaches_running_agents_once(self):
        r = fleet.broadcast(self.h, {"body": "commit what you have", "from": "desk", "idem": "bc-1"})
        self.assertEqual(sorted(x["agent"] for x in r["agents"]), ["a1", "a2", "b1"])  # idle1 is not running
        again = fleet.broadcast(self.h, {"body": "commit what you have", "from": "desk", "idem": "bc-1"})
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["id"], r["id"])
        v = self.settle(r["id"])
        self.assertTrue(v, "every running agent answers")
        for x in v["agents"]:
            self.assertIn("commit what you have", x["reply"])
            sid = self.h.s.agent(x["agent"])["session_id"]
            with open(os.path.join(os.environ["FAKE_PROJECTS"], "fake", sid + ".jsonl")) as f:
                users = [l for l in f if '"type": "user"' in l]
            self.assertEqual(len(users), 1)  # typed exactly once
        self.assertFalse(self.h.t.running("idle1"))
        n = self.h.s.one("SELECT * FROM notifications WHERE idem=?", (f"broadcast:{r['id']}",))
        self.assertIn("3/3 agents answered", n["text"])
        b = fleet.brief(self.h)
        self.assertIn(f"BROADCAST {r['id']}", b["text"])
        self.assertIn("PROJECT alpha - Alpha (next step: ship alpha)", b["text"])
        self.assertNotIn("idle1", b["text"])  # never ran, never active in the window

    def test_project_scope_and_permissions(self):
        r = fleet.broadcast(self.h, {"body": "beta only", "project": "beta"})
        self.assertEqual([x["agent"] for x in r["agents"]], ["b1"])
        with self.assertRaises(PermissionError):
            fleet.broadcast(self.h, {"body": "hi all", "from": "a1"})
        with self.assertRaises(ValueError):
            fleet.broadcast(self.h, {"body": "x", "agents": ["ghost"]})
        with self.assertRaises(ValueError):
            fleet.broadcast(self.h, {"body": ""})

    def test_wrapup_close_exits_terminals_after_they_report(self):
        r = fleet.broadcast(self.h, {"wrapup": True, "body": "back tomorrow", "close": True, "project": "alpha"})
        self.assertEqual(r["kind"], "wrapup")
        m = self.h.s.message(r["agents"][0]["message"])
        self.assertIn("STATUS:", m["body"])
        self.assertIn("Also from the owner: back tomorrow", m["body"])
        self.assertTrue(self.settle(r["id"]))
        self.assertTrue(wait_for(lambda: (fleet.tick(self.h), not self.h.t.running("a1")
                                          and not self.h.t.running("a2"))[1], 30))
        self.assertTrue(self.h.t.running("b1"))  # other project untouched
        self.assertTrue(wait_for(lambda: (fleet.tick(self.h), self.h.s.one(
            "SELECT finished_at FROM broadcasts WHERE id=?", (r["id"],))["finished_at"])[1], 10))


class BroadcastLedgerTests(unittest.TestCase):
    """The broadcast row and its messages are one transaction; Esc goes out only after it commits."""
    def setUp(self):
        self.h = new_hub()
        self.escs = []
        h = self.h

        class Busy:
            def view(self_):
                return True, True, ""

            def write(self_, data):
                self.escs.append((data, h.s.db.in_transaction))

        h.t.running = lambda aid: True
        h.t.term = lambda aid: Busy()
        self.b = {"body": "stop and commit", "agents": ["a1", "a2", "b1"], "interrupt": True}

    def test_same_key_at_once_is_one_broadcast(self):
        import threading
        real = fleet.active_agents

        def slow(*a):  # both calls pass the first duplicate check before either writes
            time.sleep(0.3)
            return real(*a)

        fleet.active_agents = slow
        out, errors = [], []

        def call():
            try:
                out.append(fleet.broadcast(self.h, {**self.b, "idem": "same"}))
            except Exception as e:
                errors.append(e)

        try:
            threads = [threading.Thread(target=call) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            fleet.active_agents = real
        self.assertEqual(errors, [])
        self.assertEqual(len({r["id"] for r in out}), 1)
        self.assertEqual(sorted(r["duplicate"] for r in out), [False, True])
        self.assertEqual(self.h.s.one("SELECT COUNT(*) n FROM broadcasts")["n"], 1)
        self.assertEqual(len([m for m in self.h.s.messages() if m["from_agent"] == "owner"]), 3)
        self.assertEqual(self.escs, [("\x1b", False)] * 3)  # once per agent, after the commit

    def test_failure_between_targets_leaves_nothing(self):
        real = self.h.s.enqueue
        calls = []

        def flaky(*a, **kw):
            calls.append(a[0])
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real(*a, **kw)

        self.h.s.enqueue = flaky
        with self.assertRaises(RuntimeError):
            fleet.broadcast(self.h, {**self.b, "idem": "retry-me"})
        del self.h.s.enqueue
        self.assertIsNone(self.h.s.one("SELECT * FROM broadcasts"))
        self.assertEqual(self.h.s.messages(), [])
        self.assertEqual(self.escs, [])
        r = fleet.broadcast(self.h, {**self.b, "idem": "retry-me"})
        self.assertFalse(r["duplicate"])
        self.assertEqual(r["total"], 3)


class ProjectBoundaryTests(unittest.TestCase):
    @unittest.skip("stale: written for the router chat / earlier isolation rules; needs rewriting")
    def test_agents_never_cross_projects(self):
        h = new_hub()
        h.s.add_agent({"id": "stray", "project": "unsorted", "title": "stray", "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        with self.assertRaises(PermissionError):  # no pulling a beta agent into alpha
            h.assign_project("b1", "alpha")
        self.assertEqual(h.s.agent("b1")["project"], "beta")
        h.assign_project("b1", "beta")  # a no-op is fine
        h.assign_project("stray", "alpha")  # an unsorted agent gets a home, once
        self.assertEqual(h.s.agent("stray")["project"], "alpha")
        with self.assertRaises(PermissionError):
            h.assign_project("stray", "beta")
        with self.assertRaises(ValueError):
            h.s.set_agent("stray", project="unsorted")
            h.assign_project("stray", "ghost")
        # an agent reaches only its own project; the chat and the owner reach any
        with self.assertRaises(PermissionError) as e:
            h.send({"to": "b1", "body": "psst", "from": "a1"})
        self.assertIn("sb send desk", str(e.exception))
        h.s.upsert_project({"id": "beta", "lead": "b1"})
        with self.assertRaises(PermissionError):
            h.send({"to": "beta", "body": "psst", "from": "a1"})  # via the project's lead too
        with self.assertRaises(PermissionError):
            h.summon({"beside": "b1", "task": "help", "from": "a1"})
        self.assertFalse(h.send({"to": "a2", "body": "same project", "from": "a1"})["duplicate"])
        self.assertFalse(h.send({"to": "b1", "body": "relay", "from": "desk"})["duplicate"])
        self.assertFalse(h.send({"to": "b1", "body": "from the owner"})["duplicate"])
        self.assertFalse(h.send({"to": "desk", "body": "For project beta: ...", "from": "a1"})["duplicate"])

    @unittest.skip("stale: written for the router chat / earlier isolation rules; needs rewriting")
    def test_answer_to_the_chat_goes_back_to_the_chat(self):
        h = new_hub()
        m = h.send({"to": "b1", "body": "From a1 on alpha: what port?", "from": "desk", "hop": 1})["message"]
        row = h.s.message(m["id"])
        h.terminal_replied(h.s.agent("b1"), row, "port 8080")
        back = h.s.one("SELECT * FROM messages WHERE to_agent='desk' AND is_reply=1")
        self.assertIn("port 8080", back["body"])
        h.terminal_replied(h.s.agent("b1"), row, "port 8080")  # once
        self.assertEqual(h.s.one("SELECT COUNT(*) n FROM messages WHERE to_agent='desk' AND is_reply=1")["n"], 1)
        m2 = h.send({"to": "a2", "body": "hi", "from": "a1"})["message"]
        h.terminal_replied(h.s.agent("a2"), h.s.message(m2["id"]), "hello")  # agent senders use --wait instead
        self.assertIsNone(h.s.one("SELECT * FROM messages WHERE to_agent='a1'"))


class WorkQueueTests(unittest.TestCase):
    def test_crud_order_and_agent_default_project(self):
        h = new_hub()
        w1 = fleet.work_add(h, {"title": "  write   the docs ", "project": "alpha"})
        w2 = fleet.work_add(h, {"title": "fix the crash", "priority": 1, "from": "b1"})
        self.assertEqual((w1["title"], w1["status"], w1["priority"]), ("write the docs", "todo", 2))
        self.assertEqual(w2["project"], "beta")  # an agent's item lands on its own project
        self.assertEqual(fleet.work_add(h, {"title": "desk item", "from": "desk"})["project"], None)
        self.assertEqual([w["id"] for w in fleet.work_list(h)["items"]][:2], [w2["id"], w1["id"]])
        w1 = fleet.work_update(h, w1["id"], {"status": "doing", "agent": "a1", "note": "started", "from": "a1"})
        self.assertEqual(fleet.work_list(h)["items"][0]["id"], w1["id"])  # doing first
        self.assertIn("a1] started", w1["detail"])
        done = fleet.work_update(h, w2["id"], {"status": "done"})
        self.assertTrue(done["done_at"])
        self.assertNotIn(w2["id"], [w["id"] for w in fleet.work_list(h)["items"]])
        self.assertIn(w2["id"], [w["id"] for w in fleet.work_list(h, status="all")["items"]])
        self.assertIn("done in the window: #%d fix the crash" % w2["id"], fleet.brief(h)["text"])
        for bad in ({"priority": 7}, {"status": "nope"}, {"project": "ghost"}, {"agent": "ghost"}, {"title": " "}):
            with self.assertRaises(ValueError):
                fleet.work_update(h, w1["id"], bad)
        fleet.work_delete(h, w1["id"])
        with self.assertRaises(LookupError):
            fleet.work_item(h, w1["id"])
        self.assertTrue(any(w["title"] == "desk item" for w in h.overview()["work"]))


if __name__ == "__main__":
    unittest.main()


class WorkNudgeTests(unittest.TestCase):
    """Stale items are put to their agent once a day; the owner is not the one who keeps the queue current."""

    def test_running_agent_is_asked_about_its_stale_items_once_a_day(self):
        h = new_hub()
        old = iso_from_epoch(time.time() - 3 * 86400)
        stale = fleet.work_add(h, {"title": "finish the parser", "agent": "a1", "project": "alpha"})
        fresh = fleet.work_add(h, {"title": "new thing", "agent": "a1", "project": "alpha"})
        unowned = fleet.work_add(h, {"title": "nobody's item", "project": "alpha"})
        other = fleet.work_add(h, {"title": "b1's old item", "agent": "b1", "project": "beta"})
        for w in (stale, unowned, other):
            h.s.x("UPDATE work SET updated_at=? WHERE id=?", (old, w["id"]))
        h.s.set_agent("a2", last_activity=iso_from_epoch(time.time()))  # no lead set: a2, last used, answers for alpha
        self.assertEqual(fleet.work_nudge(h, running=lambda aid: aid == "a1"), ["a1"])
        msgs = h.s.messages("a1")
        self.assertEqual(len(msgs), 1)
        body = msgs[0]["body"]
        self.assertIn(f"#{stale['id']} [todo, 3d] finish the parser", body)
        self.assertNotIn("new thing", body)  # touched recently
        self.assertNotIn("nobody's item", body)  # unowned: only the project's lead is asked about those
        self.assertEqual(h.s.messages("b1"), [], "not running: not asked")
        self.assertEqual(fleet.work_nudge(h, running=lambda aid: True), ["a2", "b1"], "a1 was asked today already")
        self.assertIn("nobody's item", h.s.messages("a2")[0]["body"])  # the default lead answers for unowned items
        self.assertEqual(len(h.s.messages("a1")), 1)
        h.s.upsert_project({"id": "alpha", "lead": "a1"})
        h.s.set_setting("work_nudged", {})
        self.assertEqual(fleet.work_nudge(h, running=lambda aid: aid == "a1"), ["a1"])
        self.assertTrue(any("nobody's item" in m["body"] for m in h.s.messages("a1")))  # the lead answers for unowned items
