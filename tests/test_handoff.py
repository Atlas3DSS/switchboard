"""Account handoff: the usage-limit notice is recognised, the right account is picked (Fable weekly only for Fable,
the reserve only when nothing else has room, stale readings never), an agent stopped on a limit moves and is told to continue exactly once, an agent
near its limit moves at its next turn boundary (idle, before a message is typed, before a launch).
Run: python3 -m unittest tests.test_handoff
"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbhand-")
os.environ.setdefault("SB_STATE", os.path.join(TMP, "state"))
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))
os.environ.setdefault("SB_CLAUDE_BIN", os.path.join(HERE, "fake_cli.py"))
os.environ.setdefault("FAKE_PROJECTS", os.path.join(TMP, "projects"))
os.environ.setdefault("FAKE_CLI_LOG", os.path.join(TMP, "cli.jsonl"))
os.environ.setdefault("FAKE_BUSY_S", "0.5")

from switchboard import discovery, handoff, transcript  # noqa: E402
from switchboard import config  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402

discovery.CLAUDE_PROJECTS = os.environ["FAKE_PROJECTS"]  # same as other test modules when run together
PLANS = os.path.join(TMP, "plans.json")
json.dump({"accounts": [{"name": n, "kind": "claude", "config_dir": os.path.join(TMP, "cfg-" + n)} for n in ("one", "two", "spare")]},
          open(PLANS, "w"))
CFG = {"enabled": True, "at": 95, "reserve": ["spare"]}


class PlansMixin:
    """Swap in this file's accounts only while its tests run (other test modules have their own)."""
    def setUp(self):
        self._plans = config.PATH
        config.PATH = PLANS
        super().setUp()

    def tearDown(self):
        super().tearDown()
        config.PATH = self._plans


def ts(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t or time.time()))


def acct(*limits, stale=False):
    return {"limits": [{"key": k, "used": u} for k, u in limits], "read_at": "2026-11-01T00:00:00Z", "stale": stale,
            "error": None}


def limit_line(kind="seven_day", t=None):
    return {"type": "assistant", "uuid": f"u-{time.time()}", "timestamp": ts(t), "apiError": "usage_limit_reached",
            "apiErrorParams": {"rate_limit_info": {"rateLimitType": kind}},
            "message": {"model": "<synthetic>", "role": "assistant",
                        "content": [{"type": "text", "text": "You've hit your weekly limit · resets Nov 4"}]}}


def wait_for(fn, timeout=30.0, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def launches():
    try:
        with open(os.environ["FAKE_CLI_LOG"]) as f:
            return [json.loads(l) for l in f]
    except OSError:
        return []


class DetectAndPickTests(PlansMixin, unittest.TestCase):
    def test_limit_notice_is_the_last_word(self):
        p = os.path.join(TMP, "t.jsonl")
        rows = [{"type": "user", "timestamp": ts(), "message": {"role": "user", "content": "do it"}},
                {"type": "assistant", "timestamp": ts(), "message": {"model": "claude-opus-5-5", "content": [
                    {"type": "text", "text": "working"}]}},
                limit_line()]
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        hit = transcript.limit_hit(p)
        self.assertEqual((hit["kind"], hit["text"][:20]), ("seven_day", "You've hit your week"))
        with open(p, "a") as f:  # a later synthetic note and a meta line do not hide it
            f.write(json.dumps({"type": "assistant", "timestamp": ts(), "message": {
                "model": "<synthetic>", "content": [{"type": "text", "text": "No response requested."}]}}) + "\n")
            f.write(json.dumps({"type": "user", "isMeta": True, "timestamp": ts(), "message": {"content": "x"}}) + "\n")
        self.assertTrue(transcript.limit_hit(p))
        with open(p, "a") as f:  # someone spoke after it: no longer stopped on it
            f.write(json.dumps({"type": "user", "timestamp": ts(), "message": {"content": "continue"}}) + "\n")
        self.assertIsNone(transcript.limit_hit(p))
        self.assertIsNone(transcript.limit_hit(None))

    def test_pick(self):
        s = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db"))
        use = {"one": acct(("weekly", 96), ("fable", 10)), "two": acct(("weekly", 40), ("fable", 100)),
               "spare": acct(("weekly", 0))}
        opus = {"id": "x", "provider": "claude", "account": "one", "model": "claude-opus-5-5"}
        fable = {**opus, "model": "claude-fable-5-1"}
        self.assertEqual(handoff.why_move(s, opus, use, CFG), ("one at 96%", "two"))  # not spare, though emptier
        self.assertEqual(handoff.why_move(s, fable, use, CFG), ("one at 96%", "spare"))  # two's Fable weekly is spent
        use["spare"] = acct(("weekly", 0), ("fable", 96))
        self.assertIsNone(handoff.why_move(s, fable, use, CFG))  # the reserve is out too
        use["spare"] = acct(("weekly", 0), stale=True)
        self.assertEqual(handoff.why_move(s, fable, use, CFG)[1], "spare")  # a reserve's stale reading still counts
        self.assertIsNone(handoff.why_move(s, fable, use, {**CFG, "reserve": []}))  # a non-reserve's does not
        use["spare"] = acct(("weekly", 0))
        self.assertEqual(handoff.why_move(s, fable, use, {**CFG, "reserve": []})[1], "spare")
        use["one"] = acct(("weekly", 50), ("fable", 97))
        self.assertIsNone(handoff.why_move(s, opus, use, CFG))  # Fable weekly does not limit Opus
        self.assertEqual(handoff.why_move(s, fable, use, {**CFG, "reserve": []}), ("one at 97%", "spare"))
        self.assertIsNone(handoff.why_move(s, fable, use, {**CFG, "reserve": [], "parked": ["spare"]}))  # parked: never picked
        use["two"] = acct(("weekly", 10), stale=True)
        use["one"] = acct(("weekly", 99))
        self.assertEqual(handoff.why_move(s, opus, use, CFG)[1], "spare")  # two's stale reading is not trusted
        self.assertIsNone(handoff.why_move(s, opus, use, {**CFG, "enabled": False}))


class AntigravityQuotaTests(unittest.TestCase):
    def test_quota_hit_is_read_from_the_cli_log(self):
        from switchboard import antigravity
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "log"))
        now = time.time()
        stamp = time.strftime("%m%d %H:%M:%S", time.localtime(now - 600))
        with open(os.path.join(d, "log", "cli-x.log"), "w") as f:
            f.write("I0101 00:00:00.000001 1 a.go:1] fine\n"
                    f"E{stamp}.000001 7 errorreport.go:307] generating: RESOURCE_EXHAUSTED (code 429): Individual quota "
                    "reached. Resets in 2h0m30s.\n")
        hit = antigravity.quota_hit(d, now=now)
        self.assertAlmostEqual(hit["at"], now - 600, delta=2)
        self.assertAlmostEqual(hit["resets"], now - 600 + 7230, delta=2)
        self.assertIsNone(antigravity.quota_hit(tempfile.mkdtemp(dir=TMP), now=now))
        self.assertEqual(handoff.used({"limits": [{"key": "quota", "used": None}]}, "m"), 0)


class MoveTests(PlansMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        self.use = {"one": acct(("weekly", 50)), "two": acct(("weekly", 20)), "spare": acct(("weekly", 0))}
        self.h.handoff.usage = lambda: self.use
        self.h.s.set_setting("handoff", CFG)
        self.aid = "w" + str(int(time.time() * 1000) % 10_000_000)
        self.h.s.add_agent({"id": self.aid, "project": "p", "title": "w", "provider": "claude", "account": "one",
                            "model": "claude-opus-5-5", "mode": "terminal", "session_id": None, "cwd": TMP,
                            "state": "idle"})

    def tearDown(self):
        if self.h.t.running(self.aid):
            self.h.t.exit_cli(self.aid)
        super().tearDown()

    def test_gather_moves_every_agent_to_one_account(self):
        self.h.s.add_agent({"id": self.aid + "b", "project": "p", "title": "b", "provider": "claude", "account": "spare",
                            "model": "claude-opus-5-5", "mode": "terminal", "session_id": None, "cwd": TMP,
                            "state": "stopped"})
        r = self.h.gather("two")
        self.assertEqual(sorted(r["moved"]), [self.aid, self.aid + "b"])  # neither is running: set at once
        self.assertEqual({self.h.s.agent(i)["account"] for i in r["moved"]}, {"two"})
        self.assertEqual(self.h.default_accounts()["claude"], "two")
        self.h.s.set_setting("gathering", {self.aid: "one"})  # one left waiting moves on a tick once it is not busy
        self.h.gather_tick()
        self.assertEqual(self.h.s.agent(self.aid)["account"], "one")
        self.assertEqual(self.h.s.setting("gathering"), {})
        self.assertRaises(LookupError, self.h.gather, "nobody")
        self.h.t.launch(self.h.s.agent(self.aid))  # a running one is told to carry on after its relaunch
        self.assertEqual(self.h.gather("two")["waiting"], [self.aid])
        self.h.gather_tick()
        self.assertTrue(self.h.s.one("SELECT 1 FROM messages WHERE to_agent=? AND body LIKE ?", (self.aid, "%moved every agent%")))

    def transcript(self):
        return os.path.join(os.environ["FAKE_PROJECTS"], "fake", self.h.s.agent(self.aid)["session_id"] + ".jsonl")

    def users(self):
        try:
            with open(self.transcript()) as f:
                rows = [json.loads(l) for l in f]
        except OSError:
            return []
        return [r["message"]["content"][0]["text"] for r in rows if r["type"] == "user" and
                isinstance(r["message"]["content"], list)]

    def launched_on(self):
        return [os.path.basename(l["CLAUDE_CONFIG_DIR"] or "") for l in launches() if l.get("SB_AGENT") == self.aid]

    def test_limit_mid_step_moves_and_continues_once(self):
        self.h.t.launch(self.h.s.agent(self.aid))
        self.assertTrue(wait_for(lambda: self.h.t.status(self.h.s.agent(self.aid))["state"] == "idle", 10))
        time.sleep(0.2)
        os.makedirs(os.path.dirname(self.transcript()), exist_ok=True)
        with open(self.transcript(), "a") as f:
            f.write(json.dumps(limit_line()) + "\n")
        self.use["one"] = acct(("weekly", 100))
        self.h.handoff.tick()
        self.assertTrue(wait_for(lambda: self.h.s.agent(self.aid)["account"] == "two" and self.h.t.running(self.aid)
                                 and self.aid not in self.h.t.switching, 40))
        self.assertEqual(self.launched_on(), ["cfg-one", "cfg-two"])
        self.assertTrue(wait_for(lambda: (self.h.t.tick(), any("ran out (weekly limit)" in u for u in self.users()))[1],
                                 30))
        for _ in range(3):  # the notice is behind it now: no second move, no second continue
            self.h.handoff.tick()
            self.h.t.tick()
            time.sleep(0.5)
        self.assertEqual(sum("ran out" in u for u in self.users()), 1)
        self.assertEqual(self.launched_on(), ["cfg-one", "cfg-two"])
        self.assertTrue(self.h.s.one("SELECT 1 n FROM notifications WHERE kind='handoff' AND agent=?", (self.aid,)))

    def test_near_limit_moves_at_the_turn_boundary(self):
        self.h.t.launch(self.h.s.agent(self.aid))
        self.assertTrue(wait_for(lambda: self.h.t.status(self.h.s.agent(self.aid))["state"] == "idle", 10))
        self.use["one"] = acct(("weekly", 96))
        m, _ = self.h.s.enqueue(self.aid, "next task", idem="nt-1")
        # delivery notices the account is nearly out, moves first, then types the message on the new account
        self.assertTrue(wait_for(lambda: (self.h.t.tick(), self.h.s.message(m["id"])["state"] == "done")[1], 60))
        self.assertEqual(self.h.s.agent(self.aid)["account"], "two")
        self.assertEqual(self.launched_on(), ["cfg-one", "cfg-two"])
        self.assertEqual(self.users(), ["next task"])  # no continue: nothing was cut off

    def test_stopped_agent_starts_on_an_account_with_room(self):
        self.use["one"] = acct(("weekly", 100))
        self.h.t.launch(self.h.s.agent(self.aid))
        self.assertEqual(self.h.s.agent(self.aid)["account"], "two")
        self.assertTrue(wait_for(lambda: self.launched_on(), 10))  # the CLI records its own start a moment later
        self.assertEqual(self.launched_on(), ["cfg-two"])

    def test_owner_toggle_after_a_limit_continues(self):
        self.h.t.launch(self.h.s.agent(self.aid))
        self.assertTrue(wait_for(lambda: self.h.t.status(self.h.s.agent(self.aid))["state"] == "idle", 10))
        os.makedirs(os.path.dirname(self.transcript()), exist_ok=True)
        with open(self.transcript(), "a") as f:
            f.write(json.dumps(limit_line("five_hour")) + "\n")
        self.h.s.set_setting("handoff", {**CFG, "enabled": False})  # the owner toggles by hand
        self.h.switch_account(self.aid, "spare", confirm=True)
        self.assertTrue(wait_for(lambda: (self.h.t.tick(), any("ran out (session limit)" in u for u in self.users()))[1],
                                 40))


if __name__ == "__main__":
    unittest.main()
