"""CPU-only checks for the parts that must never regress: exactly-once enqueue, hop limits, routing rules."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SB_STATE"] = tempfile.mkdtemp(prefix="sbtest-")
os.environ["SB_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="sbcfg-"), "accounts.json")
os.environ["SB_WORKSPACE"] = tempfile.mkdtemp(prefix="sbws-")

from switchboard import config, runner  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(os.path.join(tempfile.mkdtemp(), "hub.db"))

    def test_same_idem_is_one_message(self):
        a, d1 = self.s.enqueue("x", "hello", idem="k1")
        b, d2 = self.s.enqueue("x", "different text", idem="k1")
        self.assertFalse(d1)
        self.assertTrue(d2)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.s.messages()), 1)

    def test_same_body_within_ten_minutes_is_one_message(self):
        a, _ = self.s.enqueue("x", "hello", idem="k1")
        b, dup = self.s.enqueue("x", "hello", idem="k2")
        self.assertTrue(dup)
        self.assertEqual(a["id"], b["id"])

    def test_failed_message_can_be_sent_again(self):
        a, _ = self.s.enqueue("x", "hello", idem="k1")
        self.s.x("UPDATE messages SET state='failed' WHERE id=?", (a["id"],))
        b, dup = self.s.enqueue("x", "hello", idem="k2")
        self.assertFalse(dup)
        self.assertNotEqual(a["id"], b["id"])


class HubTests(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(Store(os.path.join(tempfile.mkdtemp(), "hub.db")))
        self.hub.s.add_agent({"id": "a", "mode": "managed", "provider": "claude", "account": "main",
                              "session_id": "s-a", "state": "idle"})
        self.hub.s.add_agent({"id": "b", "mode": "managed", "provider": "claude", "account": "main",
                              "session_id": "s-b", "state": "idle"})

    def test_hop_limit(self):
        with self.assertRaises(PermissionError):
            self.hub.send({"to": "a", "body": "x", "from": "b", "hop": 7})

    def test_reply_routed_once_and_never_back(self):
        m, _ = self.hub.s.enqueue("b", "question", frm="a", idem="q")
        self.hub.r.route_replies(self.hub.s.agent("b"), [self.hub.s.message(m["id"])], "answer")
        self.hub.r.route_replies(self.hub.s.agent("b"), [self.hub.s.message(m["id"])], "answer")
        to_a = self.hub.s.messages(agent="a", state="queued")
        replies = [x for x in to_a if x["to_agent"] == "a"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["is_reply"], 1)
        self.hub.r.route_replies(self.hub.s.agent("a"), [replies[0]], "thanks")
        self.assertEqual(len([x for x in self.hub.s.messages(agent="b") if x["to_agent"] == "b"]), 1)

    def test_paused_project_holds_its_agents(self):
        self.hub.s.upsert_project({"id": "p1", "name": "P1", "paused": True})
        self.hub.s.set_agent("a", project="p1")
        self.hub.s.enqueue("a", "work", idem="w")
        started = []
        self.hub.r.start_turn = lambda agent: started.append(agent["id"])
        self.hub.r.tick()
        self.assertEqual(started, [])
        self.hub.s.upsert_project({"id": "p1", "paused": False})
        self.hub.r.tick()
        self.assertEqual(started, ["a"])

    def test_waited_send_is_not_routed(self):
        m, _ = self.hub.s.enqueue("b", "question", frm="a", idem="q", wait_reply=True)
        self.hub.r.route_replies(self.hub.s.agent("b"), [self.hub.s.message(m["id"])], "answer")
        self.assertEqual([x for x in self.hub.s.messages(agent="a") if x["to_agent"] == "a"], [])

    def test_project_match_prefers_longest_prefix(self):
        self.hub.s.upsert_project({"id": "web", "name": "Web", "match": {"cwd": ["/x/web"]}})
        self.hub.s.upsert_project({"id": "api", "name": "API", "match": {"cwd": ["/x/web/api"]}})
        self.assertEqual(self.hub.project_for("/x/web/api/handlers"), "api")
        self.assertEqual(self.hub.project_for("/x/web/src"), "web")
        self.assertEqual(self.hub.project_for("/x/webX"), "unsorted")


    def test_project_without_folder_gets_one_in_workspace(self):
        from switchboard import hub as hubmod
        self.hub.s.upsert_project({"id": "newthing", "name": "New thing"})
        a = self.hub.create_agent({"provider": "shell", "project": "newthing"})
        want = os.path.join(config.workspace(), "newthing")
        self.assertEqual(a["cwd"], want)
        self.assertTrue(os.path.isdir(want))
        self.assertEqual(self.hub.s.project("newthing")["match"]["cwd"], [want])

    def test_project_folder_is_kept_and_default_is_workspace(self):
        from switchboard import hub as hubmod
        own = tempfile.mkdtemp()
        self.hub.s.upsert_project({"id": "has-dir", "name": "Has dir", "match": {"cwd": [own]}})
        self.assertEqual(self.hub.create_agent({"provider": "shell", "project": "has-dir"})["cwd"], own)
        self.assertFalse(os.path.exists(os.path.join(config.workspace(), "has-dir")))
        self.assertEqual(self.hub.create_agent({"provider": "shell"})["cwd"], config.workspace())


class CommandTests(unittest.TestCase):
    def test_claude_first_turn_then_resume(self):
        a = {"id": "x", "provider": "claude", "session_id": "sid", "model": "haiku", "permission_mode": "auto"}
        self.assertIn("--session-id", runner.turn_command(a, "f", True))
        self.assertIn("--resume", runner.turn_command(a, "f", False))

    def test_codex_resume_uses_thread(self):
        a = {"provider": "codex", "session_id": "tid", "model": "gpt-test", "cwd": "/tmp"}
        argv = runner.turn_command(a, "f", False)
        self.assertEqual(argv[:3], ["codex", "exec", "resume"])
        self.assertIn("tid", argv)

    def test_agent_messages_get_reply_footer(self):
        p = runner.compose_prompt([{"id": "m1", "from_agent": "x", "body": "hi", "is_reply": 0}])
        self.assertIn("delivered back to each sender automatically", p)
        self.assertEqual(runner.compose_prompt([{"id": "m1", "from_agent": "owner", "body": "hi", "is_reply": 0}]),
                         "hi")


class ScreenViewTests(unittest.TestCase):
    """Term.view(): whether a terminal is ready for a typed message, from its mirrored screen."""
    def view(self, data):
        from switchboard.terminals import Term
        t = Term("x", {})
        t.screen.feed(data.replace("\n", "\r\n").encode())
        return t.view()

    IDLE_FOOTER = ("● done.\n" + "─" * 20 + "\n❯ \n" + "─" * 20 +
                   "\n  ⏵⏵ auto mode on (shift+tab to cycle)\n")

    def test_idle_claude(self):
        self.assertEqual(self.view(self.IDLE_FOOTER), (True, False, ""))

    def test_old_scrollback_does_not_block(self):
        old = "✻ Waiting for 1 background agent to finish\n" + "text\n" * 30
        self.assertEqual(self.view(old + self.IDLE_FOOTER), (True, False, ""))

    def test_running_turn_is_busy(self):
        busy = self.IDLE_FOOTER.replace("● done.", "✻ Thinking… (esc to interrupt)")
        self.assertTrue(self.view(busy)[1])

    def test_owner_draft_and_placeholder(self):
        self.assertEqual(self.view(self.IDLE_FOOTER.replace("❯ ", "❯ half typed thought"))[2], "half typed thought")
        self.assertEqual(self.view(self.IDLE_FOOTER.replace("❯ ", '❯ Try "edit <filepath> to..."'))[2], "")

    def test_gemini_boxed_composer(self):
        box = "╭" + "─" * 30 + "╮\n│ >   Type your message or @path/to/file │\n╰" + "─" * 30 + "╯\n"
        self.assertEqual(self.view(box), (True, False, ""))
        self.assertEqual(self.view(box.replace("Type your message or @path/to/file", "fix it"))[2], "fix it")

    def test_builtin_agent_reports_through_the_title(self):
        self.assertEqual(self.view("\x1b]0;sb:busy\x07❯ do the thing\nworking\n"), (True, True, ""))
        self.assertEqual(self.view("\x1b]0;sb:idle\x07done\n❯ "), (True, False, ""))


if __name__ == "__main__":
    unittest.main()
