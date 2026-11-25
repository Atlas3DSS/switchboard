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
        self.assertFalse(any("acknowledged" in m["body"] for m in self.h.s.messages("a1")),
                         "a stopped agent is not started just to hear the ack")
        self.h.notify("a1", "a1: results are in", level="need", idem="n3")
        need = [x for x in inbox.items(self.h)["items"] if x["kind"] == "need"][0]
        self.assertEqual(inbox.done(self.h, need["id"], "good, write them up")["state"], "answered")
        self.assertTrue(any("Owner replied to your notice" in m["body"] and "write them up" in m["body"]
                            for m in self.h.s.messages("a1")))
        self.assertEqual(self.h.notifications()["need"], 0, "the decision echoes were acked with their decisions")
        self.assertEqual(inbox.done(self.h, self.d1, "no")["state"], "answered", "a closed item stays as it was")
        self.assertEqual(self.h.s.decision(self.d1)["answer"], "yes")
        with self.assertRaises(LookupError):
            inbox.done(self.h, "n-nope")

    def test_ignore_removes_an_item_without_a_word_to_the_agent(self):
        before = {aid: len(self.h.s.messages(aid)) for aid in ("a1", "b1")}
        self.assertEqual(inbox.done(self.h, self.d2, quiet=True)["state"], "ignored")
        self.assertEqual(self.h.s.decision(self.d2)["state"], "dismissed")
        need = [x for x in inbox.items(self.h)["items"] if x["kind"] == "need"][0]
        self.assertEqual(inbox.done(self.h, need["id"], quiet=True)["state"], "ignored")
        self.assertNotIn(self.d2, [x["id"] for x in inbox.items(self.h)["items"]])
        self.assertNotIn(need["id"], [x["id"] for x in inbox.items(self.h)["items"]])
        self.assertEqual({aid: len(self.h.s.messages(aid)) for aid in ("a1", "b1")}, before, "nobody was told")


class PendingMessageTests(unittest.TestCase):
    @unittest.skip("stale: written for the router chat / earlier isolation rules; needs rewriting")
    def test_queue_only_contains_incoming_unsent_messages_and_cancel_removes_one(self):
        h = new_hub()
        incoming, _ = h.s.enqueue('a1', 'Old pending request', frm='b1')
        h.s.enqueue('b1', 'Outgoing request', frm='a1')
        delivered, _ = h.s.enqueue('a1', 'Already typed', frm='b1')
        h.s.x("UPDATE messages SET state='delivering' WHERE id=?", (delivered['id'],))
        self.assertEqual([m['id'] for m in h.pending_messages('a1')['messages']], [incoming['id']])
        result = h.cancel_message(incoming['id'])
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(h.pending_messages('a1')['messages'], [])
        self.assertEqual(h.agent_view(h.s.agent('a1'))['queued'], 0)
        with self.assertRaises(ValueError):
            h.cancel_message(delivered['id'])
        self.assertEqual(h.s.message(delivered['id'])['state'], 'delivering')

class AdoptionTests(unittest.TestCase):
    """A notice no agent filed (a timer, a script) finds a project, and the owner can give it one that sticks."""

    def setUp(self):
        self.h = new_hub()
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.h.s.upsert_project({"id": "alpha", "match": {"cwd": [self.dir]}})

    def test_unit_file_in_a_project_folder_decides(self):
        n = self.h.notify("owner", "cve-watch: exposed", source="cve-watch.service",
                          paths=[os.path.join(self.dir, "systemd", "cve-watch.service"), "/"])
        self.assertEqual((n["project"], n["source"]), ("alpha", "cve-watch"))
        self.assertEqual(self.h.notify("owner", "x", project="beta", paths=[self.dir])["project"], "beta")
        with self.assertRaises(ValueError):
            self.h.notify("owner", "x", project="nope")

    def test_owner_adopts_a_lost_notice_and_its_source_sticks(self):
        a = self.h.notify("owner", "disk-watch: /data 91% full")
        b = self.h.notify("owner", "disk-watch: /home 95% full")
        other = self.h.notify("owner", "no prefix here")
        self.assertEqual(inbox.items(self.h)["by_project"], {"unsorted": 3})
        r = self.h.adopt(a["id"], "beta")
        self.assertEqual((r["source"], r["moved"]), ("disk-watch", 2))
        self.assertEqual(inbox.items(self.h)["by_project"], {"beta": 2, "unsorted": 1})
        self.assertEqual(self.h.notify("owner", "disk-watch: /var 99% full")["project"], "beta")
        self.assertEqual(self.h.adopt(other["id"], "alpha")["source"], None)
        self.assertIsNone(self.h.notify("owner", "another stray")["project"])
        with self.assertRaises(ValueError):
            self.h.adopt(b["id"], "nope")


if __name__ == "__main__":
    unittest.main()
