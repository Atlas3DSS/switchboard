"""Ghost agents: sealed inside their project, invisible to other agents, erased when removed (switchboard/ghost.py)."""
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_memory import Base, line  # noqa: E402  (sets SB_STATE before switchboard is imported)

from switchboard import barrier, ghost, memory, shared, terminals  # noqa: E402


class GhostTests(Base):
    def ghost(self, aid="g1", pid="alpha"):
        sid = str(uuid.uuid4())
        with open(self.path(sid), "w") as f:
            f.write(line("user", "the one task", "2026-11-08T10:00:00Z"))
            f.write(line("assistant", "done it " * 200, "2026-11-08T10:01:00Z"))
        self.hub.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "main",
                              "mode": "terminal", "session_id": sid, "cwd": self.tmp, "state": "idle",
                              "model": "claude-opus-5-5", "ghost": 1})
        return sid

    def test_sealed_and_unseen(self):
        self.agent("a1", "alpha")
        self.ghost()
        me = self.hub.s.agent("g1")
        for method, path in (("GET", "/api/context"), ("GET", "/api/memory"), ("GET", "/api/projects/alpha/memory"),
                             ("POST", "/api/notes"), ("POST", "/api/messages"), ("POST", "/api/work"),
                             ("GET", "/api/agents/a1/context"), ("GET", "/api/agents"), ("GET", "/api/overview"),
                             ("POST", "/api/helpers"), ("GET", "/api/work")):
            self.assertFalse(ghost.may(method, path, me), path)
        for method, path in (("POST", "/api/notifications"), ("POST", "/api/decisions"), ("GET", "/api/fleet"),
                             ("GET", "/api/agents/g1")):
            self.assertTrue(ghost.may(method, path, me), path)
        self.assertEqual([a["id"] for a in self.hub.project_agents("alpha")], ["a1"])
        self.assertNotIn("g1", [a["id"] for a in self.hub.project_context("alpha", "a1")["agents"]])
        self.assertNotEqual(self.hub.lead_of("alpha"), "g1")
        with self.assertRaises(LookupError):
            self.hub.send({"to": "g1", "body": "hello", "from": "a1"})
        note = shared.launch_note(me)
        self.assertIn("ghost agent", note)
        self.assertNotIn("alpha", note)  # not even the project's name
        self.assertTrue(terminals.memory_dir(me).startswith(ghost.HOME))
        self.assertTrue(barrier.agent_may("GET", "/api/context", "alpha"))  # the others are as before

    def test_nothing_goes_to_memory_and_removal_erases_it(self):
        s = self.hub.s
        sid = self.ghost()
        me = s.agent("g1")
        self.assertIsNone(memory.session_ended(self.hub, me, sid, "cleared"))
        s.event("g1", "user", "typed")
        s.x("INSERT INTO notifications(id,agent,project,text,created_at) VALUES('n1','g1','alpha','page','2026-11-08')")
        s.x("INSERT INTO messages(id,to_agent,from_agent,body,state,created_at) VALUES('m1','g1','owner','x','done','2026')")
        os.makedirs(terminals.memory_dir(me), exist_ok=True)
        r = ghost.close(self.hub, "g1")
        self.assertEqual(r["sessions"], 1)
        self.assertFalse(os.path.exists(self.path(sid)))
        self.assertFalse(os.path.exists(os.path.join(ghost.HOME, "g1")))
        self.assertIsNone(s.agent("g1"))
        for table, col in (("events", "agent"), ("notifications", "agent"), ("messages", "to_agent")):
            self.assertEqual(s.one(f"SELECT COUNT(*) n FROM {table} WHERE {col}='g1'")["n"], 0, table)
        self.agent("a1", "alpha")
        with self.assertRaises(LookupError):  # only a ghost is erased this way
            ghost.close(self.hub, "a1")


if __name__ == "__main__":
    unittest.main()
