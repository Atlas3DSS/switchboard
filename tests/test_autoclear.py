"""Auto-clear: past its model's limit, a Claude terminal agent is cleared only at rest, and the next message carries
a one-time reminder naming the outline of the old conversation."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SB_STATE"] = tempfile.mkdtemp(prefix="sbtest-")

from switchboard import autoclear, memory  # noqa: E402
from switchboard.store import Store, iso_from_epoch  # noqa: E402


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
        if s == "\r" and self.typed[-2:-1] == ["/new"]:
            self.screen = "Where should the new conversation run?\n› 1. Use current Git worktree\n  2. Create new Git worktree"

    screen = ""

    def text(self):
        return self.screen


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

    def test_codex_limit_is_for_any_openai_model(self):
        self.assertEqual(autoclear.limit_for(self.s, "gpt-6-astra", "codex"), 125_000)
        self.assertEqual(autoclear.limit_for(self.s, None, "codex"), 125_000)
        self.assertIsNone(autoclear.limit_for(self.s, "claude-sonnet-5-5", "claude"))

    def test_codex_cleared_with_new_and_follows_the_new_thread(self):
        a = self.agent(model="gpt-6-astra", tokens=124_999, provider="codex", cwd="/w")
        autoclear.check(self.terms, a, self.t)
        self.assertEqual(self.t.typed, [])
        self.s.set_agent(a["id"], context_tokens=130_000)
        self.t.busy = True  # working: not cleared
        autoclear.check(self.terms, self.s.agent(a["id"]), self.t)
        self.assertEqual(self.t.typed, [])
        self.t.busy = False
        autoclear.check(self.terms, self.s.agent(a["id"]), self.t)
        self.assertEqual(self.t.typed, ["/new", "\r", "\r"])  # the second Enter answers "where should it run"
        cur = self.s.agent(a["id"])
        self.assertIsNone(cur["session_id"])
        self.assertEqual(cur["context_tokens"], 0)
        self.assertFalse(autoclear.holding(self.s, a["id"]))  # nothing to wait for: the next message goes in
        now = time.time()
        old = (now, "", {"id": "old-sid", "cwd": "/w", "timestamp": iso_from_epoch(now - 9000)})
        claim = lambda rows: autoclear.codex_claim(self.s, a["id"], rows, ("/w",), now - 9999)  # noqa: E731
        self.assertIsNone(claim([old]))  # the old thread is not taken again
        self.assertIn("125k mark", autoclear.reminder(self.s, cur))
        self.assertEqual(autoclear.reminder(self.s, cur), "")
        self.assertIsNone(claim([old]))  # still not, once the note is spent
        new = (now, "", {"id": "new-sid", "cwd": "/w", "timestamp": iso_from_epoch(now)})
        self.assertEqual(claim([new, old]), "new-sid")
        self.assertIsNone(autoclear.state(self.s, a["id"]))

    def test_codex_new_that_did_not_take_goes_back_to_the_old_thread(self):
        a = self.agent(model="gpt-6-sol", tokens=130_000, provider="codex", cwd="/w")
        autoclear.check(self.terms, a, self.t)
        late = time.time() + autoclear.CODEX_STILL_S + 5
        rows = [(late, "", {"id": "old-sid", "cwd": "/w", "timestamp": iso_from_epoch(late - 9000)})]
        self.assertEqual(autoclear.codex_claim(self.s, a["id"], rows, ("/w",), 0), "old-sid")
        self.assertEqual(autoclear.state(self.s, a["id"])["phase"], "failed")

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

    def test_antigravity_limits_and_clear(self):
        self.assertEqual(autoclear.limit_for(self.s, "gemini-4-neon-high", "antigravity"), 470_000)
        self.assertEqual(autoclear.limit_for(self.s, "gemini-4-carbon-high", "antigravity"), 750_000)
        a = self.agent(model="gemini-4-neon-high", tokens=469_999, provider="antigravity", cwd="/w")
        autoclear.check(self.terms, a, self.t)
        self.assertEqual(self.t.typed, [])
        self.s.set_agent(a["id"], context_tokens=480_000)
        autoclear.check(self.terms, self.s.agent(a["id"]), self.t)
        self.assertEqual(self.t.typed, ["/clear", "\r"])
        cur = self.s.agent(a["id"])
        self.assertIsNone(cur["session_id"])
        self.assertEqual(cur["context_tokens"], 0)
        self.assertFalse(autoclear.holding(self.s, a["id"]))
        note = autoclear.reminder(self.s, cur)
        self.assertIn("470k mark", note)
        self.assertEqual(autoclear.state(self.s, a["id"])["phase"], "moved")

    def test_autoclear_captures_full_formatted_tail_for_owner_pane_once(self):
        self.s.upsert_project({"id": "p", "name": "P"})
        tf = os.path.join(tempfile.mkdtemp(), "rollout.jsonl")
        full_reply = ("**Summary heading**\n\n"
                      "- Item one with [plan](/tmp/plan.md:42)\n"
                      "- Item two with `code` and https://example.com/a\n\n"
                      "Second paragraph preserved in full " + ("x" * 500))
        with open(tf, "w") as f:
            f.write(json.dumps({"type": "event_msg", "timestamp": "2026-11-24T00:00:00Z",
                                "payload": {"type": "user_message", "message": "status?"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "timestamp": "2026-11-24T00:00:05Z",
                                "payload": {"type": "agent_message", "message": full_reply}}) + "\n")
        a = self.agent(model="gpt-6-astra", tokens=130_000, provider="codex", cwd="/w")
        with mock.patch.object(memory.discovery, "transcript_path", return_value=tf):
            autoclear.check(self.terms, a, self.t)
        cur = self.s.agent(a["id"])
        r = memory.recap(self.s, cur)
        self.assertIsNotNone(r)
        self.assertTrue(r["auto"])
        self.assertEqual(r["last_tail"], full_reply)
        self.assertIn("\n\n", r["last_tail"])
        self.assertIn("[plan](/tmp/plan.md:42)", r["last_tail"])
        # Dismissed once -> gone
        memory.recap_seen(self.s, a["id"], r["n"])
        self.assertIsNone(memory.recap(self.s, cur))

        # Claude auto-clear via follow_cleared -> autoclear.followed also marks auto=True and exposes full tail
        # even when project memory is switched off (fallback setting)
        memory.set_settings(self.s, {"enabled": False})
        c_tf = os.path.join(tempfile.mkdtemp(), "claude.jsonl")
        with open(c_tf, "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-11-24T00:01:00Z",
                                "message": {"role": "assistant", "content": [
                                    {"type": "text", "text": "Part 1 [doc](/tmp/a.md)"},
                                    {"type": "text", "text": "Part 2 **bold**"},
                                ]}}) + "\n")
        a2 = self.agent(model="claude-opus-5-5", tokens=500_000, provider="claude", cwd="/w", session_id="claude-old-sid")
        autoclear.check(self.terms, a2, FakeTerm())
        with mock.patch.object(memory.discovery, "transcript_path", return_value=c_tf):
            autoclear.followed(self.s, a2, "claude-old-sid", None)
        self.s.set_agent(a2["id"], session_id="new-sid", context_tokens=0)
        r2 = memory.recap(self.s, self.s.agent(a2["id"]))
        self.assertIsNotNone(r2)
        self.assertTrue(r2["auto"])
        self.assertEqual(r2["last_tail"], "Part 1 [doc](/tmp/a.md)\n\nPart 2 **bold**")
        memory.recap_seen(self.s, a2["id"], r2["n"])
        self.assertIsNone(memory.recap(self.s, self.s.agent(a2["id"])))


if __name__ == "__main__":
    unittest.main()
