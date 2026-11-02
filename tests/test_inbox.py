"""The owner's inbox: open decisions and needs per project; answering, dismissing and acknowledging cross them off
and tell the asking agent; a decision cannot be filed under an unknown agent's name; the overview carries a badge.
Run: python3 -m unittest tests.test_inbox
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbinbox-")
os.environ.setdefault("SB_STATE", os.path.join(TMP, "state"))
os.environ.setdefault("SB_CONFIG", os.path.join(TMP, "accounts.json"))
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))
os.environ.setdefault("SB_CLAUDE_BIN", os.path.join(HERE, "fake_cli.py"))
os.environ.setdefault("SB_CODEX_BIN", os.path.join(HERE, "fake_cli.py"))

from switchboard import inbox  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store, new_id, now  # noqa: E402


def new_hub():
    h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
    h.s.upsert_project({"id": "alpha", "name": "Alpha"})
    h.s.upsert_project({"id": "beta", "name": "Beta"})
    for aid, pid in (("a1", "alpha"), ("b1", "beta")):
        h.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "one",
                       "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
    return h


def ask(h, agent, question, options=(), project=None):
    did = new_id("d")
    project = project or h.s.agent(agent)["project"]
    h.s.x("INSERT INTO decisions(id,project,agent,question,options,created_at) VALUES(?,?,?,?,?,?)",
          (did, project, agent, question, __import__("json").dumps(list(options)), now()))
    h.notify(agent, f"asks: {question}", kind="decision", idem=f"decision:{did}", project=project)
    return did


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        self.d1 = ask(self.h, "a1", "Ship it?", ["yes", "no"])
        self.d2 = ask(self.h, "b1", "Which colour?", ["red", "blue"])
        self.h.notify("a1", "needs you: approve the paid call", level="need", idem="n1")
        self.h.notify("b1", "fyi only", level="info", idem="n2")

    def test_items_per_project_without_duplicates(self):
        allv = inbox.items(self.h)
        self.assertEqual(allv["count"], 3, allv)  # two decisions, one need; the info and the decision echo are not items
        self.assertEqual(allv["by_project"], {"alpha": 2, "beta": 1})
        kinds = {(x["project"], x["kind"]) for x in allv["items"]}
        self.assertEqual(kinds, {("alpha", "decision"), ("alpha", "need"), ("beta", "decision")})
        alpha = inbox.items(self.h, "alpha")
        self.assertEqual([x["agent"] for x in alpha["items"]], ["a1", "a1"])
        self.assertTrue(all(x["age_s"] is not None for x in alpha["items"]))
        self.assertEqual(self.h.overview()["inbox"], {"count": 3, "by_project": {"alpha": 2, "beta": 1}})

    def test_answer_dismiss_and_ack_cross_off_and_tell_the_agent(self):
        r = inbox.done(self.h, self.d1, "yes")
        self.assertEqual(r["state"], "answered")
        self.assertEqual(self.h.s.decision(self.d1)["answer"], "yes")
        msgs = [m["body"] for m in self.h.s.messages("a1")]
        self.assertTrue(any("Owner answered your question" in m for m in msgs), msgs)
        r = inbox.done(self.h, self.d2)
        self.assertEqual(r["state"], "dismissed")
        self.assertEqual(self.h.s.decision(self.d2)["state"], "dismissed")
        self.assertTrue(any("closed your question" in m["body"] for m in self.h.s.messages("b1")))
        need = [x for x in inbox.items(self.h)["items"] if x["kind"] == "need"][0]
        r = inbox.done(self.h, need["id"])
        self.assertEqual(r["state"], "acked")
        self.assertEqual(inbox.items(self.h)["count"], 0)
        self.assertEqual(self.h.notifications()["need"], 0, "the decision echoes were acked with their decisions")
        self.assertEqual(inbox.done(self.h, self.d1, "no")["state"], "answered", "a closed item stays as it was")
        self.assertEqual(self.h.s.decision(self.d1)["answer"], "yes")
        with self.assertRaises(LookupError):
            inbox.done(self.h, "n-nope")


if __name__ == "__main__":
    unittest.main()
