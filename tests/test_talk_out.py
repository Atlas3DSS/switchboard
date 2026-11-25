"""A project the owner gave outside write access talks to other projects through their leads; outside read access
reads their context; anyone may answer an agent that wrote first; everyone else stays inside their project."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="sb-talkout-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_SYSTEMD_USER_DIR"] = os.path.join(TMP, "units")
os.environ["SB_HOSTS"] = os.path.join(TMP, "hosts.json")
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))

from switchboard import shared  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class TalkOutTests(unittest.TestCase):
    def setUp(self):
        self.h = h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        for pid in ("alpha", "beta"):
            h.s.upsert_project({"id": pid, "name": pid})
        for aid, pid, model in (("a1", "alpha", "claude-fable-5-2"), ("b-lead", "beta", "claude-opus-5-5"),
                                ("b-arch", "beta", "claude-fable-5-2")):
            h.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "one",
                           "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
            h.s.set_agent(aid, model=model)

    def test_without_the_grant_a_project_stays_inside(self):
        with self.assertRaises(PermissionError) as e:
            self.h.send({"to": "beta", "body": "hi", "from": "a1"})
        self.assertIn("sb outside", str(e.exception))
        with self.assertRaises(PermissionError):
            self.h.project_context("beta", "a1")

    def test_outside_write_reaches_the_other_projects_lead_and_the_lead_may_answer(self):
        self.h.s.set_outside("alpha", write=True)
        self.assertEqual(self.h.send({"to": "beta", "body": "hi", "from": "a1"})["message"]["to"], "b-lead")
        self.assertEqual(self.h.send({"to": "b-arch", "body": "you directly", "from": "a1"})["message"]["to"], "b-lead")
        self.assertEqual(self.h.send({"to": "a1", "body": "answer", "from": "b-lead"})["message"]["to"], "a1")  # a reply
        with self.assertRaises(PermissionError):
            self.h.send({"to": "a1", "body": "unasked", "from": "b-arch"})
        self.h.s.hold_cross_project_messages()
        self.assertEqual([m["state"] for m in self.h.s.messages("b-lead")], ["queued", "queued", "queued"])
        with self.assertRaises(LookupError) as e:
            self.h.send({"to": "other", "body": "hi", "from": "a1"})
        self.assertIn("beta", str(e.exception))

    def test_gone_agent_with_project_id_does_not_shadow_project_routing(self):
        # A removed agent whose id matches a project id (e.g. gone 'ops') must not block `sb send ops`
        self.h.s.add_agent({"id": "beta", "project": "beta", "title": "old", "provider": "claude", "account": "one",
                            "mode": "terminal", "session_id": None, "cwd": TMP, "state": "stopped"})
        self.h.s.set_agent("beta", gone=1)
        self.h.s.set_outside("alpha", write=True)
        self.assertEqual(self.h.send({"to": "beta", "body": "still reaches lead", "from": "a1"})["message"]["to"], "b-lead")

    def test_outside_read_reads_another_projects_context(self):
        self.h.s.set_outside("alpha", read=True)
        self.assertEqual(self.h.project_context("beta", "a1")["project"], "beta")
        with self.assertRaises(PermissionError):
            self.h.send({"to": "beta", "body": "hi", "from": "a1"})

    def test_agents_are_told_they_can_talk_out(self):
        text = shared.outside_text({"read": True, "write": True})
        self.assertIn("sb send NAME", text)
        self.assertIn("sb context --project NAME", text)
        self.assertNotIn("sb send", shared.outside_text({"read": True, "write": False}))


if __name__ == "__main__":
    unittest.main()
