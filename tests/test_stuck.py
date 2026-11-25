"""Stuck at rest: an Antigravity terminal that asks for /compact gets it typed; a turn that died on an API error
pages the owner."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SB_STATE"] = tempfile.mkdtemp(prefix="sbtest-")

from switchboard import stuck  # noqa: E402
from switchboard.store import Store  # noqa: E402

ASK = """> [from agent-b via sb] Lead: OWNER addendum to round 12d

▸ Thought for 3s, 624 tokens

  I cannot successfully launch the Round 12b and 12d tasks until the conversation history is reduced.

  Please run the /compact command now. Once you have done this, I will have the necessary context space.

────────────────────────────────────────────────────────────
>
"""
WRAPPED = """> go on

  The swarm agent has failed twice due to context limits. To ensure we can proceed, I recommend you run the
  /compact command. This will summarize our conversation history.

────────────
>
"""
ERR = """> [from agent-b via sb] Lead: OWNER rule

● Used ExitPlanMode (ctrl+o to expand)

⚠ INVALID_ARGUMENT (code 400): Thinking level is unsupported: THINKING_LEVEL_XHIGH
Error ID: 6dab2dc0-48f4-4a5f-b084-94c958626eeb-1726

>
"""
TOO_BIG = ERR.replace("Thinking level is unsupported: THINKING_LEVEL_XHIGH",
                      "The request could not be processed by the model. Please retry; if the error persists, "
                      "reduce the size of the prompt or conversation history.")
QUOTA = ERR.replace("INVALID_ARGUMENT (code 400): Thinking level is unsupported: THINKING_LEVEL_XHIGH",
                    "RESOURCE_EXHAUSTED (code 429): quota reached. Resets in 2h3m.")
DONE = ASK + "> /compact\n\n──── Conversation compacted ────\n      191 steps summarized\n\n────────────\n>\n"
TALK = "> what does /compact do?\n\n  The /compact command summarizes the history. You can check it in the docs.\n\n>\n"


class FakeTerm:
    def __init__(self, screen):
        self.alive, self.busy, self.comp, self.ask, self.screen = True, False, "", None, screen
        self.last_output, self.still, self.typed = time.time() - 60, 60, []

    def view(self):
        return True, self.busy, self.comp

    def dialog(self):
        return self.ask

    def steady_for(self):
        return self.still

    def write(self, s):
        self.typed.append(s)

    def text(self):
        return self.screen


class FakeTerms:
    def __init__(self, s):
        self.s, self.switching, self.stuck, self.pages = s, {}, {}, []

    def page(self, aid, text, idem):
        self.pages.append((aid, text, idem))


class Stuck(unittest.TestCase):
    def setUp(self):
        self.s = Store(os.path.join(tempfile.mkdtemp(), "hub.db"))
        self.terms = FakeTerms(self.s)
        stuck.NUDGE_S = 0.05

    def agent(self, provider="antigravity"):
        aid = f"a{time.time_ns()}"
        self.s.add_agent({"id": aid, "project": "p", "title": aid, "provider": provider, "account": "antigravity",
                          "mode": "terminal", "session_id": "sid"})
        return self.s.agent(aid)

    def test_read(self):
        self.assertEqual(stuck.read(ASK)[0], "compact")
        self.assertEqual(stuck.read(WRAPPED)[0], "compact")
        self.assertEqual(stuck.read(TOO_BIG)[0], "compact")
        self.assertEqual(stuck.read(ERR), ("error", "INVALID_ARGUMENT (code 400): Thinking level is unsupported: "
                                                    "THINKING_LEVEL_XHIGH"))
        quoted = ERR.replace("\n>\n", "\n\n  That error in your paste came from the model setting.\n  It is fixed now.\n  Carrying on.\n\n>\n")
        for screen in (QUOTA, DONE, TALK, quoted, "", "no composer here"):
            self.assertIsNone(stuck.read(screen))

    def test_compact_is_typed_once_then_the_agent_is_told(self):
        a, t = self.agent(), FakeTerm(ASK)
        stuck.check(self.terms, a, t)
        self.assertEqual(t.typed, ["/compact", "\r"])
        time.sleep(0.3)
        ms = self.s.q("SELECT body, from_agent FROM messages WHERE to_agent=?", (a["id"],))
        self.assertEqual(len(ms), 1)
        self.assertIn("Switchboard ran it", ms[0]["body"])
        stuck.check(self.terms, a, t)  # the same screen is not judged twice
        self.assertEqual(len(t.typed), 2)
        t.last_output = time.time() - 30  # it answered, and asks again: the owner is paged, nothing more is typed
        stuck.check(self.terms, a, t)
        self.assertEqual(len(t.typed), 2)
        self.assertIn("asks for /compact again", self.terms.pages[0][1])

    def test_only_at_rest(self):
        a = self.agent()
        for change in ({"busy": True}, {"comp": "half typed"}, {"ask": "asks for permission"}, {"still": 3},
                       {"last_output": time.time()}):
            t = FakeTerm(ASK)
            t.__dict__.update(change)
            stuck.check(self.terms, a, t)
            self.assertEqual(t.typed, [], change)
        t = FakeTerm(ASK)
        stuck.check(self.terms, self.agent("claude"), t)
        self.assertEqual(t.typed, [])

    def test_api_error_pages_the_owner(self):
        a, t = self.agent(), FakeTerm(ERR)
        stuck.check(self.terms, a, t)
        self.assertEqual(t.typed, [])
        self.assertEqual(len(self.terms.pages), 1)
        self.assertIn("THINKING_LEVEL_XHIGH", self.terms.pages[0][1])

    def test_xhigh_refusal_relaunches_on_the_agents_own_model(self):
        # picked in the terminal's /model, the line long scrolled off: the hub still has the agent on High
        class Hub:
            def __init__(self):
                self.relaunched, self.notes = [], []

            def relaunch(self, aid, b):
                self.relaunched.append((aid, b))

            def notify(self, aid, text, **k):
                self.notes.append(text)

        self.terms.hub = Hub()
        a, t = self.agent(), FakeTerm(ERR)
        self.s.set_agent(a["id"], model="gemini-4-neon-high")
        stuck.check(self.terms, self.s.agent(a["id"]), t)
        time.sleep(0.3)
        self.assertEqual(self.terms.hub.relaunched, [(a["id"], {"model": "gemini-4-neon-high", "confirm": True})])
        self.assertEqual(self.terms.pages, [])
        ms = self.s.q("SELECT body FROM messages WHERE to_agent=?", (a["id"],))
        self.assertIn("XHigh", ms[0]["body"])


if __name__ == "__main__":
    unittest.main()
