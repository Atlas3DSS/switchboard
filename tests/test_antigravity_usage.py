"""Antigravity subscription usage: `agy -p "/usage"` parsed into limits, and what the account card makes of them."""
import unittest
from unittest import mock

from switchboard import accounts, antigravity

OUT = ("Gemini Models\tWeekly Limit Remaining\t87%\t2026-11-24T23:28:09Z\n"
       "Gemini Models\tFive Hour Limit Remaining\t92%\t2026-11-19T01:52:29Z\n"
       "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-26T01:27:48Z\n"
       "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-11-19T06:27:48Z\n")


class Usage(unittest.TestCase):
    def setUp(self):
        antigravity._usage.clear()

    def test_parse(self):
        got = antigravity.parse_usage("Quota:\n" + OUT + "not a limit\tx\ty\tz\n")
        self.assertEqual(len(got), 4)
        self.assertEqual(got[0], {"group": "Gemini Models", "window": "weekly", "left": 87,
                                  "resets": "2026-11-24T23:28:09Z"})
        self.assertEqual((got[1]["window"], got[1]["left"]), ("session", 92))
        self.assertEqual(antigravity.parse_usage(""), [])

    def test_one_reading_is_kept_and_a_failure_too(self):
        calls = []
        run = lambda: calls.append(1) or OUT
        got, at = antigravity.subscription_usage("/x", now=1000, run=run)
        self.assertEqual((len(got), at), (4, 1000))
        self.assertEqual(antigravity.subscription_usage("/x", now=1100, run=run)[1], 1000)  # the kept reading
        self.assertEqual(len(calls), 1)
        antigravity.subscription_usage("/x", now=1000 + antigravity.USAGE_TTL_S + 1, run=run)
        self.assertEqual(len(calls), 2)

        def fail():
            calls.append(1)
            raise OSError("no agy")
        self.assertEqual(antigravity.subscription_usage("/y", now=1000, run=fail), (None, None))
        self.assertEqual(antigravity.subscription_usage("/y", now=1001, run=fail), (None, None))
        self.assertEqual(len(calls), 3)  # the failure was kept: agy was not run again

    def card(self, out, hit=None):
        acct = [{"name": "antigravity", "kind": "antigravity", "config_dir": "/x"}]
        subs = antigravity.parse_usage(out)
        with mock.patch.object(accounts.discovery, "accounts", return_value=acct), \
                mock.patch.object(antigravity, "quota_hit", return_value=hit), \
                mock.patch.object(antigravity, "subscription_usage", return_value=(subs or None, 1000 if subs else None)):
            return accounts.usage()["antigravity"]

    def test_card_shows_used_not_remaining(self):
        card = self.card(OUT)
        self.assertEqual([(l["key"], l["label"], l["used"]) for l in card["limits"]],
                         [("session", "Session (5h)", 8), ("weekly", "Weekly", 13)])  # the unused Claude/GPT pair is left out
        self.assertEqual(card["limits"][1]["resets"], "2026-11-24T23:28:09Z")
        self.assertTrue(card["read_at"])

    def test_the_other_pair_shows_once_used_and_an_old_failure_only_without_a_reading(self):
        import time
        card = self.card(OUT.replace("Claude and GPT models\tWeekly Limit Remaining\t100%", "Claude and GPT models\tWeekly Limit Remaining\t60%"))
        self.assertEqual([(l["key"], l["used"]) for l in card["limits"]], [("session", 8), ("weekly", 13), ("other_weekly", 40)])
        old = {"at": time.time() - 7200, "resets": time.time() + 7200}
        self.assertNotIn("quota", [l["key"] for l in self.card(OUT, old)["limits"]])
        self.assertEqual([l["key"] for l in self.card("", old)["limits"]], ["quota"])  # agy gave nothing: the last failure
        fresh = {"at": time.time() - 60, "resets": time.time() + 7200}
        self.assertEqual(self.card(OUT, fresh)["limits"][-1]["used"], 100)

class XHigh(unittest.TestCase):
    """The CLI offers XHigh for models the API refuses it for; the hub lowers it to High."""

    def test_the_high_variant_and_the_remembered_refusal(self):
        self.assertEqual(antigravity.high_for("gemini-4-neon-xhigh"), "gemini-4-neon-high")
        self.assertIsNone(antigravity.high_for("gemini-4-carbon-high"))
        with mock.patch.dict(antigravity._labels, {"agy": (9e12, {"gemini-4-neon-high": "Gemini 4 Neon (High)"})}):
            screen = "> /model\n  Model set to Gemini 4 Neon (XHigh)\n⚠ INVALID_ARGUMENT (code 400): Thinking level is unsupported"
            self.assertEqual(antigravity.high_for("gemini-4-carbon-high", screen), "gemini-4-neon-high")
            self.assertEqual(antigravity.model_arg("gemini-4-neon-xhigh"), "gemini-4-neon-xhigh")
            with mock.patch.object(antigravity, "NO_XHIGH", {"gemini-4-neon"}):
                self.assertEqual(antigravity.model_arg("gemini-4-neon-xhigh"), "Gemini 4 Neon (High)")


class AntigravityHarness(unittest.TestCase):
    """Context windows, per-session settings, and /clear session following."""

    def test_context_window_and_session_settings(self):
        self.assertEqual(antigravity.context_window_for("gemini-4-neon-high"), 512_000)
        self.assertEqual(antigravity.context_window_for("gemini-pro-orchestrator-neon"), 512_000)
        self.assertEqual(antigravity.context_window_for("gemini-4-carbon-high"), 1_000_000)
        s_in = antigravity.session_settings(outside_access=False)
        self.assertEqual(s_in["artifactReviewPolicy"], "always-proceed")
        self.assertEqual(s_in["queuedMessages"], "send-immediately")
        self.assertFalse(s_in["showTips"])
        self.assertNotIn("allowNonWorkspaceAccess", s_in)
        self.assertTrue(antigravity.session_settings(outside_access=True)["allowNonWorkspaceAccess"])

    def test_follow_session_matches_prompt_on_screen(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.join(d, "repo")
            os.makedirs(ws)
            now_ms = int(1_700_000_100 * 1000)
            with open(os.path.join(d, "history.jsonl"), "w") as f:
                f.write(json.dumps({"display": "old prompt text here", "timestamp": now_ms - 50000,
                                    "workspace": ws, "conversationId": "old-cid"}) + "\n")
                f.write(json.dumps({"display": "other agent prompt in same repo", "timestamp": now_ms,
                                    "workspace": ws, "conversationId": "other-cid"}) + "\n")
                f.write(json.dumps({"display": "fresh prompt after clear in this pane", "timestamp": now_ms + 1000,
                                    "workspace": ws, "conversationId": "new-cid"}) + "\n")
            screen = "> fresh prompt after clear in this pane\n\nWorking on it..."
            self.assertEqual(antigravity.follow_session(ws, "old-cid", screen, since=1_700_000_000, d=d), "new-cid")
            # Already on new-cid even when old-cid text is also visible: never bounce backward to old-cid
            both = "old prompt text here\n> fresh prompt after clear in this pane\n"
            self.assertIsNone(antigravity.follow_session(ws, "new-cid", both, since=1_700_000_000, d=d))
            self.assertIsNone(antigravity.follow_session(ws, "old-cid", screen, since=1_700_000_000,
                                                         taken={"new-cid"}, d=d))
            self.assertIsNone(antigravity.follow_session(ws, "old-cid", "unrelated screen text",
                                                         since=1_700_000_000, d=d))

    def test_antigravity_busy_detection_for_queued_messages_and_idle_footer(self):
        from switchboard.terminals import Term

        def view_for(screen_text):
            t = Term("a", {})
            t.provider = "antigravity"
            t.screen.feed(screen_text.replace("\n", "\r\n"))
            return t.view()

        # Normal working footer
        self.assertEqual(view_for("────────\n>\n────────\nesc to cancel")[:2], (True, True))
        # Working with agy's internal queued-message footer (replaces 'esc to cancel')
        queued = ("▸ [from agent-a via sb] go on P0... (alt+m to send immediately)\n"
                  "────────\n>\n────────\n  Press up to edit queued messages")
        self.assertEqual(view_for(queued)[:2], (True, True))
        # Idle footer wins over any stale 'esc to cancel' line above it
        idle = "esc to cancel\n────────\n>\n────────\n? for shortcuts"
        self.assertEqual(view_for(idle)[:2], (True, False))


if __name__ == "__main__":
    unittest.main()
