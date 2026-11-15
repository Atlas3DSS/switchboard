"""Owner-only history tools: an agent's memory files with who wrote them, and rewinding an agent's session."""
import json
import os
import uuid
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_memory import Base, line  # noqa: E402  (sets SB_STATE before switchboard is imported)

from switchboard import barrier, memory, rewind  # noqa: E402
from switchboard.rewind import REWINDS  # noqa: E402


def write_call(fp, ts="2026-11-08T10:00:00Z"):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": fp, "content": "x"}}]}}) + "\n"


def ts(i):
    return f"2026-11-08T10:{i:02d}:00Z"


class RewindTests(Base):
    def timed(self, aid, pid, n=6, sid=None):
        """A session with one owner message and one reply a minute, message i at 10:0i."""
        sid = sid or str(uuid.uuid4())
        with open(self.path(sid), "w") as f:
            for i in range(n):
                f.write(line("user", f"ask {i} of {aid}", ts(2 * i)))
                f.write(line("assistant", f"reply {i} of {aid}", ts(2 * i + 1)))
        if not self.hub.s.agent(aid):
            self.hub.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "main",
                                  "mode": "terminal", "session_id": sid, "cwd": self.tmp, "state": "idle",
                                  "model": "claude-opus-5-5"})
        return sid

    def test_rewind_erases_the_record_as_if_it_never_happened(self):
        s = self.hub.s
        rewind.delete_copies("a1")  # another test's copy, same agent id
        sid = self.timed("a1", "alpha")
        eid = memory.session_ended(self.hub, s.agent("a1"), sid, "cleared")
        s.x("UPDATE memory_sessions SET state='done' WHERE id=?", (eid,))
        s.x("INSERT INTO memory(project,text,updated_at) VALUES('alpha',?,?)",
            ("NOW\n- only from the cut part [#%d]\n- backed twice [#%d, n1]\n- older [#9999]" % (eid, eid), ts(0)))
        for i, when in ((1, ts(1)), (2, ts(9))):  # one before the moment (10:04), one after
            s.x("INSERT INTO messages(id,to_agent,from_agent,body,state,created_at,delivered_at) VALUES(?,?,?,?,?,?,?)",
                (f"m{i}", "a1", "owner", "x", "done", when, when))
            s.x("INSERT INTO messages(id,to_agent,from_agent,body,state,created_at) VALUES(?,?,?,?,?,?)",
                (f"o{i}", "a2", "a1", "y", "done", when))
            s.event("a1", "user", f"event {i}", ts=when)
            s.x("INSERT INTO notes(project,agent,text,created_at) VALUES('alpha','a1',?,?)", (f"note {i}", when))
            s.x("INSERT INTO notifications(id,agent,project,text,created_at) VALUES(?,?,?,?,?)",
                (f"n{i}", "a1", "alpha", "page", when))
        sub = os.path.join(self.root, "-p", sid, "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-early.jsonl"), "w") as f:
            f.write(line("user", "early", ts(1)))
        with open(os.path.join(sub, "agent-late.jsonl"), "w") as f:
            f.write(line("user", "late", ts(9)))
        mo = next(m for m in rewind.moments(self.hub, "a1")["sessions"][0]["moments"] if m["text"] == "ask 2 of a1")
        r = rewind.rewind(self.hub, "a1", {"session": sid, "offset": mo["offset"]})
        self.assertIsNone(r.get("backup"))
        self.assertFalse(os.path.exists(os.path.join(REWINDS, "a1")))
        self.assertNotIn("ask 2", open(self.path(sid)).read())
        self.assertEqual(os.listdir(sub), ["agent-early.jsonl"])
        self.assertEqual([m["id"] for m in s.q("SELECT id FROM messages ORDER BY id")], ["m1", "o1"])
        self.assertEqual([e["text"] for e in s.q("SELECT text FROM events WHERE agent='a1' AND kind='user'")], ["event 1"])
        self.assertEqual([n["text"] for n in s.q("SELECT text FROM notes")], ["note 1"])
        self.assertEqual([n["id"] for n in s.q("SELECT id FROM notifications")], ["n1"])
        self.assertIsNone(s.one("SELECT id FROM memory_sessions WHERE id=?", (eid,)))
        self.assertEqual(s.one("SELECT text FROM memory WHERE project='alpha'")["text"],
                         "NOW\n- backed twice [n1]\n- older [#9999]")
        self.assertEqual(s.agent("a1")["session_id"], sid)

    def test_rewind_into_an_earlier_session_erases_the_later_ones(self):
        old = self.timed("a1", "alpha")
        new = str(uuid.uuid4())
        self.timed("a1", "alpha", sid=new)
        os.makedirs(os.path.join(self.root, "-p", new, "tool-results"))
        memory.session_ended(self.hub, self.hub.s.agent("a1"), old, "cleared")
        self.hub.s.set_agent("a1", session_id=new)
        later = memory.session_ended(self.hub, self.hub.s.agent("a1"), new, "idle")
        mo = rewind.moments(self.hub, "a1")["sessions"][1]["moments"][0]
        rewind.rewind(self.hub, "a1", {"session": old, "offset": mo["offset"]})
        self.assertFalse(os.path.exists(self.path(new)))
        self.assertFalse(os.path.exists(os.path.join(self.root, "-p", new)))
        self.assertIsNone(self.hub.s.one("SELECT id FROM memory_sessions WHERE id=?", (later,)))
        self.assertEqual([x["session"] for x in rewind.moments(self.hub, "a1")["sessions"]], [old])

    def test_memory_files_edit_and_delete(self):
        mem = os.path.join(self.root, "-p", "memory")
        os.makedirs(mem)
        fp = os.path.join(mem, "rule.md")
        with open(fp, "w") as f:
            f.write("rule\n")
        with open(os.path.join(mem, "MEMORY.md"), "w") as f:
            f.write("- [Rule](rule.md) - a rule\n- [Other](other.md) - other\n")
        rewind.save_memfile(fp, "new rule\n")
        self.assertEqual(open(fp).read(), "new rule\n")
        self.assertEqual(rewind.delete_memfile(fp)["index_lines_removed"], 1)
        self.assertFalse(os.path.exists(fp))
        self.assertEqual(open(os.path.join(mem, "MEMORY.md")).read(), "- [Other](other.md) - other\n")
        outside = os.path.join(self.tmp, "x.md")
        open(outside, "w").close()
        for bad in (outside, self.path("nope"), os.path.join(mem, "..", "..", "x.md")):
            with self.assertRaises((PermissionError, LookupError)):
                rewind.delete_memfile(bad)

    def test_delete_outline_scrubs_memory(self):
        sid = self.agent("a1", "alpha")
        eid = self.end("a1")
        self.hub.s.x("INSERT INTO memory(project,text,updated_at) VALUES('alpha',?,?)",
                     ("- a [#%d]\n- b [#%d, #2]" % (eid, eid), ts(0)))
        rewind.delete_outline(self.hub, eid)
        self.assertEqual(self.hub.s.one("SELECT text FROM memory")["text"], "- b [#2]")

    def test_moments_newest_first_and_rewind_cuts_with_a_copy(self):
        sid = self.agent("a1", "alpha")
        eid = self.end("a1")
        v = rewind.moments(self.hub, "a1")
        ms = v["sessions"][0]["moments"]
        self.assertEqual(len(ms), 6)
        self.assertTrue(ms[0]["text"].startswith("ask 5 of a1"))
        target = ms[3]  # "ask 2"
        self.assertEqual(target["later"], 3)
        full = open(self.path(sid)).read()
        r = rewind.rewind(self.hub, "a1", {"session": sid, "offset": target["offset"], "keep_copy": True})
        self.assertFalse(r["relaunch"])
        self.assertTrue(r["message"].startswith("ask 2 of a1"))
        left = open(self.path(sid)).read()
        self.assertEqual(left, full[:target["offset"]])
        self.assertIn("reply 1 of a1", left)
        self.assertNotIn("ask 2 of a1", left)
        self.assertEqual(open(r["backup"]).read(), full)
        self.assertEqual(oct(os.stat(r["backup"]).st_mode & 0o777), "0o600")
        self.assertIsNone(self.hub.s.one("SELECT id FROM memory_sessions WHERE id=?", (eid,)))  # it outlined the cut part
        ev = self.hub.s.q("SELECT text FROM events WHERE agent='a1' AND text LIKE 'rewound by the owner%'")
        self.assertEqual(len(ev), 1)

    def test_offset_must_be_one_of_its_messages(self):
        sid = self.agent("a1", "alpha")
        with self.assertRaises(ValueError):
            rewind.rewind(self.hub, "a1", {"session": sid, "offset": 7})
        self.agent("a2", "alpha")
        with self.assertRaises(ValueError):  # another agent's session
            rewind.rewind(self.hub, "a2", {"session": sid, "offset": 0})

    def test_back_into_an_earlier_session(self):
        old = self.agent("a1", "alpha")
        memory.session_ended(self.hub, self.hub.s.agent("a1"), old, "cleared")
        new = self.agent("tmp", "alpha")  # a second transcript; then a1 moves onto it as after a /clear
        self.hub.s.x("DELETE FROM agents WHERE id='tmp'")
        self.hub.s.set_agent("a1", session_id=new)
        v = rewind.moments(self.hub, "a1")
        self.assertEqual([s["session"] for s in v["sessions"]], [new, old])
        self.assertTrue(v["sessions"][0]["current"])
        mo = v["sessions"][1]["moments"][0]
        rewind.rewind(self.hub, "a1", {"session": old, "offset": mo["offset"]})
        self.assertEqual(self.hub.s.agent("a1")["session_id"], old)

    def test_busy_agent_needs_confirm(self):
        sid = self.agent("a1", "alpha")
        off = rewind.moments(self.hub, "a1")["sessions"][0]["moments"][0]["offset"]
        with mock.patch.object(self.hub.t, "running", return_value=True), \
                mock.patch.object(self.hub.t, "busy", return_value=True):
            with self.assertRaises(rewind.NeedsConfirm):
                rewind.rewind(self.hub, "a1", {"session": sid, "offset": off})

    def test_memory_files_with_writers(self):
        mem = os.path.join(self.tmp, "mem", "memory")
        os.makedirs(mem)
        fp = os.path.join(mem, "rule.md")
        with open(fp, "w") as f:
            f.write("---\nname: rule\ndescription: a rule\nmetadata:\n  type: feedback\n---\nbody\n")
        with open(os.path.join(mem, "other.md"), "w") as f:
            f.write("no front matter\n")
        sid = self.agent("a1", "alpha")
        with open(self.path(sid), "a") as f:
            f.write(write_call(fp))
            f.write(write_call(os.path.join(mem, "gone.md")))
        self.agent("a2", "alpha")
        with mock.patch.object(rewind, "memory_dir", return_value=mem):
            rewind._dirs.clear()
            v = rewind.memory_files(self.hub, "alpha")
            by = {f["name"]: f for f in v["files"]}
            self.assertEqual(set(by), {"rule.md", "other.md", "gone.md"})
            self.assertEqual(by["rule.md"]["front"], {"name": "rule", "description": "a rule", "type": "feedback"})
            self.assertEqual([b["agent"] for b in by["rule.md"]["by"]], ["a1"])
            self.assertFalse(by["gone.md"]["exists"])
            self.assertEqual(by["other.md"]["by"], [])
            self.assertEqual({f["name"] for f in rewind.memory_files(self.hub, "alpha", "a2")["files"]}, set())
            self.assertEqual({f["name"] for f in rewind.memory_files(self.hub, "alpha", "a1")["files"]},
                             {"rule.md", "gone.md"})

    def test_owner_only(self):
        for method, path in (("GET", "/api/agents/a1/moments"), ("POST", "/api/agents/a1/rewind"),
                             ("GET", "/api/projects/alpha/memfiles"), ("POST", "/api/memfiles/save"),
                             ("POST", "/api/memfiles/delete"), ("POST", "/api/memory/3/delete"),
                             ("POST", "/api/agents/a1/rewind/copies/delete")):
            self.assertFalse(barrier.agent_may(method, path, "alpha"), path)
            self.assertTrue(barrier.agent_may(method, path, "switchboard"), path)  # the owner's own project is not limited


if __name__ == "__main__":
    unittest.main()
