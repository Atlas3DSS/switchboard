"""Every project has the same seats (lead, tasker, janitor) and a live wiki; a seat inherits a session used in the
last 38 hours and starts clean otherwise."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="sb-nodes-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_SYSTEMD_USER_DIR"] = os.path.join(TMP, "units")
os.environ["SB_HOSTS"] = os.path.join(TMP, "hosts.json")
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))

from switchboard import fleet, nodes  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store, iso_from_epoch  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        self.h.s.upsert_project({"id": "alpha", "name": "Alpha", "goal": "ship alpha", "next_step": "write the parser"})

    def agent(self, aid, model, hours_ago, provider="claude"):
        self.h.s.add_agent({"id": aid, "project": "alpha", "title": aid, "provider": provider, "account": "one",
                            "mode": "terminal", "session_id": "s-" + aid, "cwd": TMP, "state": "idle"})
        self.h.s.set_agent(aid, model=model, last_activity=iso_from_epoch(time.time() - hours_ago * 3600))

    def seat(self, role):
        return next(s for s in nodes.seats(self.h, "alpha")["seats"] if s["role"] == role)

    def test_an_idle_project_still_has_its_seats_with_the_default_models(self):
        st = nodes.seats(self.h, "alpha")
        self.assertEqual([(s["role"], s["model"], s["state"], s["pickup"]) for s in st["seats"]],
                         [("lead", "claude-opus-5-5", "empty", "clean start"),
                          ("architect", "claude-fable-5-2", "empty", "clean start"),
                          ("tasker", "claude-haiku-5-5", "empty", "clean start"),
                          ("janitor", "claude-haiku-5-5", "empty", "clean start")])
        self.assertEqual(self.seat("lead")["title"], "Lead Opus")

    def test_seats_fill_from_the_projects_agents_and_inherit_only_recent_sessions(self):
        self.agent("brain", "claude-fable-5-2", 1)
        self.agent("front", "claude-opus-5-5", 40)   # older than 38 h: a clean start
        self.agent("doer", "claude-haiku-5-5", 2)
        self.agent("sweeper", "claude-haiku-5-5", 3)
        self.agent("spare", "claude-opus-5-5", 90)
        self.assertEqual((self.seat("architect")["agent"], self.seat("architect")["pickup"]), ("brain", "inherit session"))
        self.assertEqual((self.seat("lead")["agent"], self.seat("lead")["state"], self.seat("lead")["pickup"]),
                         ("front", "stale", "clean start"))
        self.assertEqual((self.seat("tasker")["agent"], self.seat("janitor")["agent"]), ("doer", "sweeper"))
        self.assertEqual(self.h.lead_of("alpha"), "front")  # a message to the project reaches Lead Opus
        self.assertEqual([x["agent"] for x in nodes.seats(self.h, "alpha")["extras"]], ["spare"])

    def test_a_big_model_never_lands_in_a_small_seat(self):
        self.agent("boss", "claude-fable-5-2", 1)
        self.agent("worker", "claude-opus-5-5", 1)
        self.agent("third", "claude-opus-5-5", 1)
        self.assertEqual((self.seat("tasker")["agent"], self.seat("janitor")["agent"]), (None, None))
        self.agent("astra", "gpt-6-astra", 1, provider="codex")
        self.agent("terra", "gpt-6.1-terra", 1, provider="codex")
        self.assertEqual(self.seat("tasker")["agent"], "terra")
        self.assertEqual(self.seat("architect")["agent"], "astra")  # Astra suits the Architect's seat, never a small one
        self.assertEqual(len(nodes.seats(self.h, "alpha")["extras"]), 2)

    def test_owner_swaps_a_seats_model_and_agent(self):
        self.agent("boss", "claude-fable-5-2", 1)
        self.agent("worker", "gpt-5.5", 1, provider="codex")
        nodes.set_seat(self.h, "alpha", "lead", {"agent": "worker", "model": "gpt-5.5", "provider": "codex"})
        lead = self.seat("lead")
        self.assertEqual((lead["agent"], lead["model"], lead["provider"], lead["chosen"]), ("worker", "gpt-5.5", "codex", True))
        self.assertEqual(self.seat("architect")["agent"], "boss")
        with self.assertRaises(ValueError):
            nodes.set_seat(self.h, "alpha", "lead", {"agent": "nobody"})
        nodes.set_seat(self.h, "alpha", "lead", {"agent": "", "model": "", "provider": ""})
        self.assertEqual(self.seat("lead")["model"], "claude-opus-5-5")

    def test_wiki_has_goals_last_deliverable_nodes_and_sessions(self):
        self.agent("boss", "claude-opus-5-5", 1)
        w1 = fleet.work_add(self.h, {"title": "parser", "agent": "boss", "project": "alpha"})
        fleet.work_add(self.h, {"title": "docs", "project": "alpha"})
        self.h.s.x("UPDATE work SET status='done', done_at=? WHERE id=?", (iso_from_epoch(time.time()), w1["id"]))
        w = nodes.wiki(self.h, "alpha", talk=False)
        self.assertEqual(w["goals"]["goal"], "ship alpha")
        self.assertEqual(w["last_deliverable"]["work"]["title"], "parser")
        self.assertEqual([x["title"] for x in w["goals"]["open_work"]], ["docs"])
        self.assertEqual(w["sessions"]["live"][0]["seat"], "lead")
        md = nodes.markdown(w)
        for want in ("## Goals", "next step: write the parser", "## Last deliverable", "Lead Opus", "## Sessions"):
            self.assertIn(want, md)
        self.assertIn("alpha", [r["project"] for r in nodes.index(self.h)["projects"]])

    def test_brief_names_the_role_and_carries_the_wiki(self):
        b = nodes.brief(self.h, "alpha", "janitor")
        self.assertIn("You are the Janitor of project Alpha", b)
        self.assertIn("clean context", b)
        self.assertIn("ship alpha", b)

    def test_briefs_carry_the_same_facts_in_each_account_types_shape(self):
        self.assertIn("<project_wiki>", nodes.brief(self.h, "alpha", "lead", provider="claude"))
        codex = nodes.brief(self.h, "alpha", "tasker", provider="codex")
        self.assertTrue(codex.startswith("## Role"))
        self.assertIn("## Rules\n- ", codex)
        small = nodes.brief(self.h, "alpha", "janitor", provider="deepseek")
        self.assertNotIn("## Memory", small)
        for b in (codex, small):
            self.assertIn("ship alpha", b)

    def test_a_seat_lands_on_the_account_with_room_and_moves_type_when_none_has(self):
        accts = [{"name": "one", "kind": "claude"}, {"name": "two", "kind": "claude"}, {"name": "oai", "kind": "codex"}]
        lim = lambda pct: {"read_at": "x", "stale": False, "error": None, "limits": [{"key": "weekly", "used": pct}]}
        with mock.patch.object(nodes.discovery, "accounts", return_value=accts):
            self.assertEqual(nodes.pick_account(self.h, "claude", "claude-opus-5-5", {"one": lim(80), "two": lim(20)}), "two")
            self.assertEqual(nodes.place(self.h, "tasker", "claude", "claude-opus-5-5",
                                         {"one": lim(99), "two": lim(97), "oai": lim(10)}), ("codex", "gpt-6.1-terra", "oai"))
            self.assertEqual(nodes.place(self.h, "tasker", "claude", "claude-opus-5-5",
                                         {"one": lim(99), "two": lim(97), "oai": lim(100)}), (None, None, None))

    def test_pickup_inherits_a_recent_session_clears_an_old_one_and_starts_an_empty_seat(self):
        self.agent("boss", "claude-opus-5-5", 1)
        self.agent("worker", "claude-haiku-5-5", 50)
        self.assertEqual(nodes.pickup(self.h, "alpha", "lead")["action"], "inherited")
        self.assertEqual(self.h.s.messages("boss"), [])
        r = nodes.pickup(self.h, "alpha", "tasker")
        self.assertEqual((r["agent"], r["action"]), ("worker", "cleared"))
        bodies = sorted(m["body"] for m in self.h.s.messages("worker"))
        self.assertEqual(bodies[0], "/clear")
        self.assertIn("You are the Tasker of project Alpha", bodies[1])
        def made(b):
            self.h.s.add_agent({"id": "alpha-janitor", "project": "alpha", "title": "Janitor", "provider": "claude",
                                "account": "one", "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
            return {"id": "alpha-janitor"}
        with mock.patch.object(nodes, "place", return_value=("claude", "claude-haiku-5-5", "one")), \
                mock.patch.object(self.h, "create_agent", side_effect=made) as mk:
            r = nodes.pickup(self.h, "alpha", "janitor")
        self.assertEqual((r["action"], r["agent"], r["moved"]), ("started", "alpha-janitor", False))
        self.assertIn("You are the Janitor", mk.call_args[0][0]["prompt"])
        self.assertEqual(self.seat("janitor")["chosen"], True)

    def test_fold_retires_idle_agents_outside_the_seats_and_hands_their_work_to_the_lead(self):
        self.agent("boss", "claude-opus-5-5", 1)
        self.agent("worker", "claude-fable-5-2", 1)
        self.agent("spare", "claude-opus-5-5", 5)
        self.agent("midturn", "claude-opus-5-5", 6)
        w = fleet.work_add(self.h, {"title": "spare's item", "agent": "spare", "project": "alpha"})
        self.h.s.enqueue("midturn", "still to do", frm="owner")
        r = nodes.fold(self.h, "alpha")
        self.assertEqual(([f["agent"] for f in r["folded"]], r["left_busy"]), (["spare"], ["midturn"]))
        self.assertTrue(self.h.s.agent("spare")["gone"])
        self.assertFalse(self.h.s.agent("worker")["gone"])
        self.assertEqual(self.h.s.one("SELECT agent FROM work WHERE id=?", (w["id"],))["agent"], "boss")

    def test_recap_shows_the_conversation_that_ended_until_the_owner_dismisses_it(self):
        from switchboard import memory
        self.agent("boss", "claude-opus-5-5", 1)
        a = self.h.s.agent("boss")
        self.assertIsNone(memory.recap(self.h.s, a))
        outline = {"sections": [{"h": "ASKED", "points": [{"t": "fix it", "m": [1]}]},
                                {"h": "DONE", "points": [{"t": "parser fixed", "m": [4]}]}]}
        self.h.s.x("INSERT INTO memory_sessions(project,agent,provider,session_id,why,state,summary,outline,last_said,created_at) "
                   "VALUES('alpha','boss','claude','old-session','cleared automatically','done','Fixed the parser.',?,'All done.','2026-11-10T00:00:00Z')",
                   (json.dumps(outline),))
        r = memory.recap(self.h.s, a)
        self.assertEqual((r["summary"], r["sections"], r["last_said"]),
                         ("Fixed the parser.", [{"h": "DONE", "points": ["parser fixed"]}], "All done."))
        self.assertEqual(self.h.agent_view(a)["recap"]["n"], r["n"])
        memory.recap_seen(self.h.s, "boss", r["n"])
        self.assertIsNone(memory.recap(self.h.s, a))

    def test_standardize_renames_pins_and_adds_an_unpinged_lead_opus(self):
        self.agent("brain", "claude-fable-5-2", 1)
        self.agent("doer", "claude-haiku-5-5", 2)
        self.h.s.upsert_project({"id": "alpha", "lead": "brain"})  # a Fable lead becomes the Architect
        sid = self.h.s.agent("brain")["session_id"]
        with mock.patch.object(nodes, "place", return_value=("claude", "claude-opus-5-5", "one")), \
                mock.patch.object(nodes.discovery, "accounts", return_value=[{"name": "one", "kind": "claude", "config_dir": ""}]):
            r = nodes.standardize(self.h, "alpha")
        self.assertEqual({x["agent"]: x["now"] for x in r["renamed"]}, {"brain": "Architect Fable", "doer": "Tasker Haiku"})
        self.assertEqual(self.h.s.agent("brain")["session_id"], sid)  # the conversation is untouched
        lead = self.h.s.agent(r["created"]["agent"])
        self.assertEqual((lead["id"], lead["title"], lead["model"]), ("alpha-lead", "Lead Opus", "claude-opus-5-5"))
        self.assertEqual(self.h.s.messages("alpha-lead"), [])  # made, not pinged
        self.assertEqual((self.seat("lead")["agent"], self.seat("lead")["pickup"]), ("alpha-lead", "clean start"))
        self.agent("newer", "claude-fable-5-2", 0)
        self.assertEqual(self.seat("architect")["agent"], "brain")  # pinned: a more recent Fable does not take the seat
        self.h.send({"to": "alpha", "body": "hello lead", "from": "owner"})
        bodies = [m["body"] for m in sorted(self.h.s.messages("alpha-lead"), key=lambda m: m["created_at"])]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(any("You are the Lead Opus of project Alpha" in b for b in bodies))


if __name__ == "__main__":
    unittest.main()
