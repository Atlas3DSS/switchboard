"""Unit tests for mid-turn push delivery, three-stage confirmation, and the 2-minute rule (work #795).
Run: python3 -m unittest tests.test_delivery_push
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from switchboard import discovery, hub, store, terminals, transcript  # noqa: E402


class FakeTerm:
    def __init__(self, aid, provider="claude", ready=True, busy=True, comp="", dialog=None, screen="", steady=10.0):
        self.aid = aid
        self.provider = provider
        self.alive = True
        self._ready = ready
        self._busy = busy
        self._comp = comp
        self._dialog = dialog
        self._screen = screen
        self._steady = steady
        self.writes = []

    def view(self):
        return self._ready, self._busy, self._comp

    def dialog(self):
        return self._dialog

    def steady_for(self):
        return self._steady

    def text(self):
        return self._screen

    def _composer(self):
        return terminals.Term._composer(self)

    def composer_tail(self):
        return terminals.Term.composer_tail(self)

    def write(self, data):
        self.writes.append(data)


class DeliveryPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sb-push-")
        self.s = store.Store(os.path.join(self.tmp, "hub.db"))
        self.t = terminals.Terminals(self.s)
        self.pages = []
        self.t.on_page = lambda aid, text, idem: self.pages.append((aid, text, idem))
        for sender in ("peer", "lead"):
            self._add(sender, "claude", f"sid-{sender}")

    def _add(self, aid, provider="claude", sid="sid-1", paused=0):
        self.s.add_agent({"id": aid, "project": "p", "title": aid, "provider": provider, "account": "one",
                          "mode": "terminal", "session_id": sid, "cwd": self.tmp, "state": "busy", "paused": paused})
        return self.s.agent(aid)

    def test_claude_transcript_reads_human_queued_command_only(self):
        p = os.path.join(self.tmp, "s.jsonl")
        rows = [
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-23T03:21:14.000Z",
             "content": "[from opus via sb] midturn update"},
            {"type": "attachment", "timestamp": "2026-11-23T03:21:15.000Z", "isSidechain": False,
             "attachment": {"type": "queued_command", "prompt": "<task-notification>bg done</task-notification>",
                            "commandMode": "task-notification", "origin": {"kind": "task-notification"}}},
            {"type": "attachment", "timestamp": "2026-11-23T03:21:16.000Z", "isSidechain": False,
             "attachment": {"type": "queued_command", "prompt": "<agent-message from='x'>subagent</agent-message>",
                            "commandMode": "prompt", "origin": {"kind": "peer"}, "isMeta": True}},
            {"type": "attachment", "timestamp": "2026-11-23T03:21:18.000Z", "isSidechain": False,
             "attachment": {"type": "queued_command", "prompt": "[from opus via sb] midturn update",
                            "commandMode": "prompt", "origin": {"kind": "human"}, "humanTurn": True}},
        ]
        with open(p, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        chat = transcript.read_chat(p, "claude", limit=20)
        users = [e for e in chat["entries"] if e["role"] == "user"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["text"], "[from opus via sb] midturn update")

    def test_three_stage_confirmation_claude(self):
        a = self._add("c1", "claude", "sid-c1")
        p = os.path.join(self.tmp, "sid-c1.jsonl")
        open(p, "w").close()
        ft = FakeTerm("c1", "claude", busy=True)
        self.t.terms["c1"] = ft
        t0 = time.time() - 400  # well past CONFIRM_S (180 s)
        m = self.s.enqueue("c1", "pushed work", frm="peer")[0]
        self.s.x("UPDATE messages SET state='delivering', turn_id=?, delivered_at=? WHERE id=?",
                 ("pty-" + m["id"], store.iso_from_epoch(t0), m["id"]))

        with mock.patch.object(discovery, "transcript_path", return_value=p):
            # 1) enqueue seen -> accepted: stays delivering, never failed past CONFIRM_S
            with open(p, "a") as f:
                f.write(json.dumps({"type": "queue-operation", "operation": "enqueue",
                                    "timestamp": store.iso_from_epoch(t0 + 1),
                                    "content": "[from peer via sb] pushed work"}) + "\n")
            self.t._confirm_one(self.s.message(m["id"]))
            cur = self.s.message(m["id"])
            self.assertEqual(cur["state"], "delivering")
            self.assertEqual(cur["error"], terminals.ACCEPTED_WAIT)
            self.assertIsNone(cur["read_at"])

            # 2) queued_command attachment seen -> read: state becomes delivered, read_at is now() (NOT backdated enqueue time!)
            enqueue_ts = store.iso_from_epoch(t0 + 1)
            with open(p, "a") as f:
                f.write(json.dumps({"type": "attachment", "timestamp": enqueue_ts, "isSidechain": False,
                                    "attachment": {"type": "queued_command",
                                                   "prompt": "[from peer via sb] pushed work",
                                                   "humanTurn": True, "origin": {"kind": "human"}}}) + "\n")
            before_read = time.time()
            self.t._confirm_one(self.s.message(m["id"]))
            cur = self.s.message(m["id"])
            self.assertEqual(cur["state"], "delivered")
            self.assertNotEqual(cur["read_at"], enqueue_ts)
            self.assertGreaterEqual(store.epoch(cur["read_at"]), before_read - 2)
            self.assertIsNone(cur["error"])

            # msg_view exposes read_at and status_note
            mv = hub.Hub.msg_view(None, cur)
            self.assertEqual(mv["read_at"], cur["read_at"])
            self.assertIn("read at ", mv["status_note"])
            self.assertIn("reply when its turn ends", mv["status_note"])

    def test_slash_command_waits_for_idle_and_gets_esc_past_120s(self):
        a = self._add("c2", "claude")
        self.s.set_setting("delivery", {"push": True})
        ft = FakeTerm("c2", "claude", busy=True)
        self.t.terms["c2"] = ft
        m = self.s.enqueue("c2", "/compact", frm="owner")[0]
        self.t._deliver_one(a)
        self.assertEqual(ft.writes, [])
        cur = self.s.message(m["id"])
        self.assertEqual(cur["state"], "queued")
        self.assertIn("slash command waits for an idle prompt", cur["error"])
        # Past INTERRUPT_AFTER_S (120 s), slash command falls back to one Esc so the queue does not stall forever
        self.s.x("UPDATE messages SET created_at=? WHERE id=?", (store.iso_from_epoch(time.time() - 130), m["id"]))
        self.t._deliver_one(a)
        self.assertEqual(ft.writes, ["\x1b"])

    def test_antigravity_scrollback_does_not_trigger_alt_m_or_accepted(self):
        # Old scrollback 15 lines above '>' must NOT trigger \x1bm or false 'accepted'
        scroll = ("▸ old msg (alt+m to send immediately)\nPress up to edit queued messages\n"
                  + "\n".join(f"log line {i}" for i in range(15))
                  + "\n>\nWorking...")
        a = self._add("a-scroll", "antigravity")
        self.s.set_setting("delivery", {"push": True})
        ft = FakeTerm("a-scroll", "antigravity", busy=True, screen=scroll)
        self.t.terms["a-scroll"] = ft
        m = self.s.enqueue("a-scroll", "fresh msg", frm="lead")[0]
        with mock.patch("switchboard.terminals.time.sleep"):
            self.t._deliver_one(a)
        self.assertNotIn("\x1bm", ft.writes)
        # And _confirm_one must NOT mark it accepted from scrollback alone
        self.s.x("UPDATE messages SET delivered_at=? WHERE id=?", (store.iso_from_epoch(time.time() - 10), m["id"]))
        with mock.patch.object(discovery, "transcript_path", return_value=None):
            self.t._confirm_one(self.s.message(m["id"]))
        self.assertIsNone(self.s.message(m["id"])["error"])

    def test_accepted_unread_lost_escape_hatch_all_three_routes(self):
        t0 = time.time() - 60
        # Route 1: idle steady >= 30 s with attempts=1 -> requeued
        self._add("c-idle1", "claude", "sid-i1")
        ft1 = FakeTerm("c-idle1", "claude", ready=True, busy=False, comp="", steady=35.0)
        self.t.terms["c-idle1"] = ft1
        m1 = self.s.enqueue("c-idle1", "lost while idle", frm="peer")[0]
        self.s.x("UPDATE messages SET state='delivering', attempts=1, turn_id=?, delivered_at=?, error=? WHERE id=?",
                 ("pty-" + m1["id"], store.iso_from_epoch(t0), terminals.ACCEPTED_WAIT, m1["id"]))
        with mock.patch.object(discovery, "transcript_path", return_value=None):
            self.t._confirm_one(self.s.message(m1["id"]))
        cur1 = self.s.message(m1["id"])
        self.assertEqual((cur1["state"], cur1["turn_id"], cur1["error"]), ("queued", None, None))

        # Route 2: terminal gone (t is None) with attempts=1 -> requeued
        self._add("c-gone", "claude", "sid-g1")
        m2 = self.s.enqueue("c-gone", "lost on exit", frm="peer")[0]
        self.s.x("UPDATE messages SET state='delivering', attempts=1, turn_id=?, delivered_at=?, error=? WHERE id=?",
                 ("pty-" + m2["id"], store.iso_from_epoch(t0), terminals.ACCEPTED_WAIT, m2["id"]))
        with mock.patch.object(discovery, "transcript_path", return_value=None):
            self.t._confirm_one(self.s.message(m2["id"]))
        cur2 = self.s.message(m2["id"])
        self.assertEqual((cur2["state"], cur2["turn_id"], cur2["error"]), ("queued", None, None))

        # Route 3: idle steady >= 30 s with attempts=2 -> failed (never loops forever)
        self._add("c-idle2", "claude", "sid-i2")
        ft3 = FakeTerm("c-idle2", "claude", ready=True, busy=False, comp="", steady=35.0)
        self.t.terms["c-idle2"] = ft3
        m3 = self.s.enqueue("c-idle2", "lost twice", frm="peer")[0]
        self.s.x("UPDATE messages SET state='delivering', attempts=2, turn_id=?, delivered_at=?, error=? WHERE id=?",
                 ("pty-" + m3["id"], store.iso_from_epoch(t0), terminals.ACCEPTED_WAIT, m3["id"]))
        with mock.patch.object(discovery, "transcript_path", return_value=None):
            self.t._confirm_one(self.s.message(m3["id"]))
        cur3 = self.s.message(m3["id"])
        self.assertEqual(cur3["state"], "failed")
        self.assertIn("disappeared before being read", cur3["error"])

    def test_antigravity_single_write_alt_m_only_when_queue_banner(self):
        a = self._add("a1", "antigravity")
        self.s.set_setting("delivery", {"push": True})
        ft = FakeTerm("a1", "antigravity", busy=True,
                      screen="> \n▸ hi (alt+m to send immediately)\nPress up to edit queued messages")
        self.t.terms["a1"] = ft
        self.s.enqueue("a1", "urgent steer", frm="lead")
        with mock.patch("switchboard.terminals.time.sleep"):
            self.t._deliver_one(a)
        # Paste, Enter, and ONE single write of "\x1bm" (never split into "\x1b" + "m")
        self.assertIn("\x1bm", ft.writes)
        self.assertNotIn("\x1b", ft.writes)
        self.assertTrue(any(terminals.PUSH_NOTE in w for w in ft.writes))

        # Now in send-immediately mode (no alt+m hint): no \x1bm written
        a2 = self._add("a2", "antigravity")
        ft2 = FakeTerm("a2", "antigravity", busy=True, screen="> \n  ⎿  Sending to agent...\nPress up to edit queued messages")
        self.t.terms["a2"] = ft2
        self.s.enqueue("a2", "urgent steer 2", frm="lead")
        with mock.patch("switchboard.terminals.time.sleep"):
            self.t._deliver_one(a2)
        self.assertNotIn("\x1bm", ft2.writes)
        self.assertNotIn("\x1b", ft2.writes)
        self.assertTrue(any(terminals.PUSH_NOTE in w for w in ft2.writes))

    def test_two_minute_rule_codex_push_off_draft_and_dialog(self):
        self.assertEqual(terminals.INTERRUPT_AFTER_S, 120)
        self.assertEqual(terminals.PUSH_CEILING_S, 120)
        self.s.set_setting("delivery", {"push": True, "push_off": ["c-off"]})

        # 1) Codex waits under 120 s, gets one Esc past 120 s
        cx = self._add("cx", "codex")
        ft_cx = FakeTerm("cx", "codex", busy=True)
        self.t.terms["cx"] = ft_cx
        m_cx = self.s.enqueue("cx", "for codex", frm="peer")[0]
        self.s.x("UPDATE messages SET created_at=? WHERE id=?", (store.iso_from_epoch(time.time() - 130), m_cx["id"]))
        self.t._deliver_one(cx)
        self.assertEqual(ft_cx.writes, ["\x1b"])

        # 2) push_off Claude agent delays under 120 s (no Esc!), then pushes non-destructively at 120 s
        co = self._add("c-off", "claude")
        ft_co = FakeTerm("c-off", "claude", busy=True)
        self.t.terms["c-off"] = ft_co
        m_co = self.s.enqueue("c-off", "delayed msg", frm="peer")[0]
        self.s.x("UPDATE messages SET created_at=? WHERE id=?", (store.iso_from_epoch(time.time() - 30), m_co["id"]))
        self.t._deliver_one(co)
        self.assertEqual(ft_co.writes, [])  # neither typed nor Esc'd before 120 s
        self.s.x("UPDATE messages SET created_at=? WHERE id=?", (store.iso_from_epoch(time.time() - 125), m_co["id"]))
        with mock.patch("switchboard.terminals.time.sleep"):
            self.t._deliver_one(co)
        self.assertNotIn("\x1b", ft_co.writes)
        self.assertTrue(any("delayed msg" in w for w in ft_co.writes))

        # 3) Owner draft in composer: never typed over, pages owner once at 120 s
        dr = self._add("c-draft", "claude")
        ft_dr = FakeTerm("c-draft", "claude", busy=False, comp="owner typing draft")
        self.t.terms["c-draft"] = ft_dr
        m_dr = self.s.enqueue("c-draft", "held behind draft", frm="peer")[0]
        self.s.x("UPDATE messages SET created_at=? WHERE id=?", (store.iso_from_epoch(time.time() - 125), m_dr["id"]))
        self.t._deliver_one(dr)
        self.assertEqual(ft_dr.writes, [])
        self.assertTrue(any(aid == "c-draft" and "draft" in txt for aid, txt, _ in self.pages))

        # 4) Open dialog: never typed over, pages owner once at 120 s
        dl = self._add("c-dlg", "claude")
        ft_dl = FakeTerm("c-dlg", "claude", busy=False, dialog="asks for permission")
        self.t.terms["c-dlg"] = ft_dl
        m_dl = self.s.enqueue("c-dlg", "held behind dialog", frm="peer")[0]
        self.s.x("UPDATE messages SET created_at=? WHERE id=?", (store.iso_from_epoch(time.time() - 125), m_dl["id"]))
        self.t._deliver_one(dl)
        self.assertEqual(ft_dl.writes, [])
        self.assertTrue(any(aid == "c-dlg" and "open question" in txt for aid, txt, _ in self.pages))

    def test_stale_accepted_esc_for_claude_and_page_for_antigravity(self):
        # Claude accepted-but-unread > PUSH_STALE_S with owner draft in composer -> NO Esc
        a_draft = self._add("c-stale-draft", "claude", "sid-sd")
        ft_sd = FakeTerm("c-stale-draft", "claude", busy=True, comp="owner typing")
        self.t.terms["c-stale-draft"] = ft_sd
        t0 = time.time() - (terminals.PUSH_STALE_S + 30)
        md = self.s.enqueue("c-stale-draft", "stuck behind sleep", frm="peer")[0]
        self.s.x("UPDATE messages SET state='delivering', turn_id=?, delivered_at=?, error=? WHERE id=?",
                 ("pty-" + md["id"], store.iso_from_epoch(t0), terminals.ACCEPTED_WAIT, md["id"]))
        with mock.patch.object(discovery, "transcript_path", return_value=None):
            self.t._confirm_one(self.s.message(md["id"]))
        self.assertEqual(ft_sd.writes, [])

        # Claude accepted-but-unread > PUSH_STALE_S -> one Esc (not twice)
        a = self._add("c-stale", "claude", "sid-stale")
        ft = FakeTerm("c-stale", "claude", busy=True)
        self.t.terms["c-stale"] = ft
        m = self.s.enqueue("c-stale", "stuck behind sleep", frm="peer")[0]
        self.s.x("UPDATE messages SET state='delivering', turn_id=?, delivered_at=?, error=? WHERE id=?",
                 ("pty-" + m["id"], store.iso_from_epoch(t0), terminals.ACCEPTED_WAIT, m["id"]))
        with mock.patch.object(discovery, "transcript_path", return_value=None):
            self.t._confirm_one(self.s.message(m["id"]))
            self.t._confirm_one(self.s.message(m["id"]))  # second tick: no second Esc
        self.assertEqual(ft.writes, ["\x1b"])

        # Antigravity accepted-but-unread > PUSH_STALE_S -> pages owner, never Esc
        ag = self._add("a-stale", "antigravity", "sid-a")
        ft_ag = FakeTerm("a-stale", "antigravity", busy=True, screen="> \n  ⎿  Sending to agent...")
        self.t.terms["a-stale"] = ft_ag
        m2 = self.s.enqueue("a-stale", "stuck agy", frm="peer")[0]
        self.s.x("UPDATE messages SET state='delivering', turn_id=?, delivered_at=?, error=? WHERE id=?",
                 ("pty-" + m2["id"], store.iso_from_epoch(t0), terminals.ACCEPTED_WAIT, m2["id"]))
        with mock.patch.object(discovery, "transcript_path", return_value=None):
            self.t._confirm_one(self.s.message(m2["id"]))
        self.assertEqual(ft_ag.writes, [])
        self.assertTrue(any(aid == "a-stale" and "unread" in txt for aid, txt, _ in self.pages))

    def test_unaccepted_retry_enter_before_failing(self):
        a = self._add("c-retry", "claude", "sid-r")
        ft = FakeTerm("c-retry", "claude", busy=True, comp="[from peer via sb] stuck in box")
        self.t.terms["c-retry"] = ft
        t0 = time.time() - (terminals.CONFIRM_S + 10)
        m = self.s.enqueue("c-retry", "stuck in box", frm="peer")[0]
        self.s.x("UPDATE messages SET state='delivering', turn_id=?, delivered_at=? WHERE id=?",
                 ("pty-" + m["id"], store.iso_from_epoch(t0), m["id"]))
        with mock.patch.object(discovery, "transcript_path", return_value=None):
            self.t._confirm_one(self.s.message(m["id"]))
        self.assertEqual(ft.writes, ["\r"])
        self.assertEqual(self.s.message(m["id"])["state"], "delivering")


if __name__ == "__main__":
    unittest.main()
