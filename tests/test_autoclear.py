"""Auto-clear: past its model's limit, a Claude terminal agent is cleared only at rest, and the next message carries
a one-time reminder naming the outline of the old conversation."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SB_STATE"] = tempfile.mkdtemp(prefix="sbtest-")

from switchboard import autoclear  # noqa: E402
from switchboard.store import Store  # noqa: E402


class FakeTerm:
    def __init__(self):
        self.alive, self.busy, self.comp, self.bg, self.ask = True, False, "", False, None
        self.last_output, self.still, self.typed = time.time() - 60, 60, []

    def view(self):
        return True, self.busy, self.comp

    def dialog(self):
        return self.ask

    def background(self):
        return self.bg

    def steady_for(self):
        return self.still

    def write(self, s):
        self.typed.append(s)


class FakeTerms:
    def __init__(self, s):
        self.s, self.switching = s, {}


class AutoClear(unittest.TestCase):
    def setUp(self):
        self.s = Store(os.path.join(tempfile.mkdtemp(), "hub.db"))
        self.terms, self.t = FakeTerms(self.s), FakeTerm()

    def agent(self, model="claude-opus-5-5", tokens=500_000, **kw):
        aid = f"a{time.time_ns()}"
        self.s.add_agent({"id": aid, "project": "p", "title": aid, "provider": "claude", "account": "main",
                          "model": model, "mode": "terminal", "session_id": "old-sid", "context_tokens": tokens, **kw})
        return self.s.agent(aid)

    def test_limits_per_model(self):
        self.assertEqual(autoclear.limit_for(self.s, "claude-fable-5-1"), 382_000)
        self.assertEqual(autoclear.limit_for(self.s, "claude-opus-5-5"), 482_000)
        self.assertEqual(autoclear.limit_for(self.s, "claude-haiku-5-5"), 650_000)
        self.assertIsNone(autoclear.limit_for(self.s, "claude-sonnet-5-5"))

    def test_under_limit_nothing(self):
        autoclear.check(self.terms, self.agent(tokens=481_999), self.t)
        self.assertEqual(self.t.typed, [])

    def test_waits_for_rest(self):
        a = self.agent()
        for attr, val in (("busy", True), ("comp", "half typed"), ("bg", True), ("ask", "asks for permission"),
                          ("still", 3)):
            t = FakeTerm()
            setattr(t, attr, val)
            autoclear.check(self.terms, a, t)
            self.assertEqual(t.typed, [], attr)
        self.s.x("INSERT INTO messages(id,to_agent,body,state,created_at) VALUES('m1',?,'x','delivered','now')",
                 (a["id"],))
        autoclear.check(self.terms, a, self.t)
        self.assertEqual(self.t.typed, [])

    def test_clear_then_one_reminder(self):
        a = self.agent()
        autoclear.check(self.terms, a, self.t)
        self.assertEqual(self.t.typed, ["/clear", "\r"])
        self.assertTrue(autoclear.holding(self.s, a["id"]))
        autoclear.check(self.terms, a, self.t)  # not typed twice
        self.assertEqual(len(self.t.typed), 2)
        autoclear.followed(self.s, a, "old-sid", 41)
        self.assertFalse(autoclear.holding(self.s, a["id"]))
        note = autoclear.reminder(self.s, a)
        self.assertIn("sb memory 41", note)
        self.assertIn("500k", note)
        self.assertIn("482k", note)
        self.assertEqual(autoclear.reminder(self.s, a), "")

    def test_owner_clear_gets_no_reminder(self):
        a = self.agent(tokens=10)
        autoclear.followed(self.s, a, "old-sid", 7)
        self.assertEqual(autoclear.reminder(self.s, a), "")

    def test_clear_that_never_took_backs_off(self):
        a = self.agent()
        autoclear.check(self.terms, a, self.t)
        st = autoclear.state(self.s, a["id"])
        self.s.set_setting(f"autoclear:{a['id']}", {**st, "ts": time.time() - autoclear.FOLLOW_S - 1})
        autoclear.check(self.terms, a, self.t)
        self.assertEqual(autoclear.state(self.s, a["id"])["phase"], "failed")
        self.assertFalse(autoclear.holding(self.s, a["id"]))
        autoclear.check(self.terms, a, self.t)
        self.assertEqual(len(self.t.typed), 2)

    def test_helpers_skipped(self):
        autoclear.check(self.terms, self.agent(role="helper"), self.t)
        self.assertEqual(self.t.typed, [])


if __name__ == "__main__":
    unittest.main()
