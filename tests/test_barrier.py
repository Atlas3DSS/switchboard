"""The project barrier: an agent acts inside its own project, only the owner acts across projects, and what a caller
says about itself proves nothing. Run: python3 -m unittest tests.test_barrier
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="sbbarrier-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")

from switchboard import barrier, hosts, hub as hubmod, jobs  # noqa: E402
from switchboard.hub import Hub, make_handler  # noqa: E402
from switchboard.store import Store  # noqa: E402

# A client in its own process, with whatever environment the test gives it. Prints "status body".
CLIENT = """
import http.client, json, os, sys
method, path, body = sys.argv[1:4]
h = {"Content-Type": "application/json", "Host": "127.0.0.1:" + os.environ["PORT"]}
if len(sys.argv) > 4:
    h["X-Switchboard-Agent"] = sys.argv[4]
c = http.client.HTTPConnection("127.0.0.1", int(os.environ["PORT"]), timeout=20)
c.request(method, path, body, h)
r = c.getresponse()
print(r.status, r.read().decode())
"""


class Barrier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Hub(Store(os.path.join(TMP, "hub.db")))
        for aid, pid in (("chem", "labwork"), ("rat", "rats"), ("sbdev", "switchboard")):
            cls.h.s.upsert_project({"id": pid, "name": pid})
            cls.h.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "x",
                               "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.h))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_port
        cls.old = hubmod.ALLOWED_HOSTS
        hubmod.ALLOWED_HOSTS = {f"127.0.0.1:{cls.port}"}

    @classmethod
    def tearDownClass(cls):
        hubmod.ALLOWED_HOSTS = cls.old
        cls.srv.shutdown()

    def setUp(self):  # nothing in this file may reach the real hosts
        for name in ("halt", "resume", "guards", "add_host"):
            p = mock.patch.object(hosts, name, return_value={"called": name, "results": []})
            setattr(self, name, p.start())
            self.addCleanup(p.stop)

    def call(self, method, path, body=None, agent=None):
        """From this very process: the hub's own threads, so only the header decides."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Content-Type": "application/json"}
        if agent:
            h["X-Switchboard-Agent"] = agent
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read().decode()
        c.close()
        return r.status, json.loads(data) if data else None

    def shell(self, script, *argv, env=None):
        """A client run the way an agent's Bash tool runs one: a shell carrying the agent's environment, that may then
        change the environment of what it starts."""
        e = {k: v for k, v in os.environ.items() if not k.startswith("SB_")}
        e.update(PORT=str(self.port), SB_HUB=f"http://127.0.0.1:{self.port}", CLIENT=CLIENT, **(env or {}))
        # The trailing command keeps this shell alive above the client, as the agent's CLI always is (bash would
        # otherwise exec its last command and the agent would be gone from the ancestry).
        r = subprocess.run(["bash", "-c", script + "\nrc=$?; exit $rc", "bash", *argv], env=e, capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip() + r.stderr.strip()

    # ---------- fleet-wide commands ----------
    def test_an_agent_cannot_halt(self):
        for path in ("halt", "resume", "guards", "hosts"):
            status, res = self.call("POST", f"/api/fleet/{path}", {}, agent="chem")
            self.assertEqual(status, 409, path)
            self.assertIn("only the owner", res["error"])
        self.halt.assert_not_called()
        self.resume.assert_not_called()

    def test_the_owner_can_halt(self):
        self.assertEqual(self.call("POST", "/api/fleet/halt", {"reason": "x"})[0], 200)
        self.halt.assert_called_once()
        self.assertEqual(self.call("POST", "/api/fleet/resume", {})[0], 200)

    def test_removing_or_forging_the_claim_does_not_help(self):
        agent = {"SB_AGENT": "chem"}
        halt = ('POST', '/api/fleet/halt', '{}')
        for label, script, extra in (
                ("header dropped", 'python3 -c "$CLIENT" "$@"', ()),
                ("env unset", 'env -u SB_AGENT python3 -c "$CLIENT" "$@"', ()),
                ("env forged", 'SB_AGENT=owner python3 -c "$CLIENT" "$@"', ("owner",)),
                ("another agent claimed", 'SB_AGENT=rat python3 -c "$CLIENT" "$@"', ("rat",)),
                ("hub address changed", 'SB_HUB=http://elsewhere:1 python3 -c "$CLIENT" "$@"', ())):
            out = self.shell(script, *halt, *extra, env=agent)
            self.assertTrue(out.startswith("409"), f"{label}: {out}")
            self.assertIn("only the owner", out, label)
        self.halt.assert_not_called()

    def test_detaching_from_the_agent_does_not_help(self):
        out = self.shell('''setsid -w sh -c 'unset SB_AGENT; setsid python3 -c "$CLIENT" "$@" > "$OUT" 2>&1 &' sh "$@"
                            for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
                              [ -s "$OUT" ] && break; sleep 0.25; done; cat "$OUT"''',
                         "POST", "/api/fleet/halt", "{}", env={"SB_AGENT": "chem", "OUT": os.path.join(TMP, "detached")})
        self.assertTrue(out.startswith("409"), out)
        self.halt.assert_not_called()

    @unittest.skip("stale: written for the router chat / earlier isolation rules; needs rewriting")
    def test_identity_comes_from_the_process_for_scoped_writes_too(self):
        """Forged owner: `sb send` across projects is still chem's."""
        out = self.shell('SB_AGENT=owner python3 -c "$CLIENT" "$@"', "POST", "/api/messages",
                         json.dumps({"to": "rat", "body": "hello"}), "owner", env={"SB_AGENT": "chem"})
        self.assertTrue(out.startswith("409"), out)
        self.assertIn("isolated", out)

    @unittest.skip("stale: written for the router chat / earlier isolation rules; needs rewriting")
    def test_the_owners_own_project_is_not_limited(self):
        """Switchboard's agents are the owner's terminal on the whole Switchboard (owner rule, 2026-11-09)."""
        st, r = self.call("GET", "/api/agents", agent="sbdev")
        self.assertEqual((st, {"chem", "rat", "sbdev"} <= {a["id"] for a in r["agents"]}), (200, True))
        self.assertEqual(self.call("GET", "/api/agents/chem/context", agent="sbdev")[0], 200)
        st, r = self.call("POST", "/api/messages", {"to": "rat", "body": "from the owner's terminal"}, agent="sbdev")
        self.assertEqual(st, 200, r)
        st, r = self.call("PATCH", "/api/agents/chem", {"title": "Chem"}, agent="sbdev")
        self.assertEqual((st, r["title"]), (200, "Chem"))
        self.assertEqual(self.call("GET", "/api/agents/rat/context", agent="chem")[0], 409)  # everyone else stays closed
        self.assertEqual(self.call("POST", "/api/messages", {"to": "rat", "body": "x"}, agent="chem")[0], 409)

    def test_sb_halt_refused_in_plain_words(self):
        out = self.shell(f'{sys.executable} {ROOT}/bin/sb halt', env={"SB_AGENT": "chem"})
        self.assertIn("only the owner", out)
        self.assertIn("sb fleet", out)
        self.halt.assert_not_called()

    def test_a_plain_owner_process_still_works(self):
        out = self.shell(f'{sys.executable} {ROOT}/bin/sb fleet --json >/dev/null; python3 -c "$CLIENT" "$@"',
                         "POST", "/api/fleet/halt", "{}")
        self.assertIn("200", out)  # no agent above it
        self.halt.assert_called_once()

    # ---------- everything else that crossed ----------
    def test_owner_only_routes(self):
        for path in ("/api/phone/pair", "/api/phone/unpair", "/api/settings", "/api/focus",
                     "/api/notifications/ack-all", "/api/notifications/n1/ack", "/api/decisions/d1/answer",
                     "/api/projects", "/api/broadcast", "/api/sessions/resume", "/api/restart"):
            status, res = self.call("POST", path, {}, agent="chem")
            self.assertEqual(status, 409, path)
            self.assertIn("only the owner", res["error"], path)

    def test_an_unlisted_route_is_closed(self):
        self.assertFalse(barrier.agent_may("POST", "/api/some/new/thing"))
        self.assertFalse(barrier.agent_may("GET", "/api/some/new/thing"))

    def test_maintainers_restart_the_hub(self):
        with mock.patch.object(self.h, "restart", return_value={"ok": True}) as r:
            self.assertEqual(self.call("POST", "/api/restart", {}, agent="rat")[0], 409)
            r.assert_not_called()
            self.assertEqual(self.call("POST", "/api/restart", {}, agent="sbdev")[0], 200)
            r.assert_called_once()

    def job(self, jid, project, agent):
        self.h.s.x("INSERT INTO jobs(id,name,project,agent,host,unit,cwd,argv,threads,mem_mib,state,created_at) "
                   "VALUES(?,?,?,?,'h',?,'/','[]',1,64,'queued','2026-01-01T00:00:00Z')",
                   (jid, jid, project, agent, f"sb-job-{jid}.service"))

    def test_jobs_stay_inside_the_project(self):
        self.job("job-rat", "rats", "rat")
        self.job("job-chem", "labwork", "chem")
        self.assertEqual(self.call("POST", "/api/jobs/job-rat/stop", {}, agent="chem")[0], 409)
        self.assertEqual(self.h.s.one("SELECT state FROM jobs WHERE id='job-rat'")["state"], "queued")
        self.assertEqual(self.call("GET", "/api/jobs/job-rat?log=0", agent="chem")[0], 409)
        listed = self.call("GET", "/api/jobs", agent="chem")[1]["jobs"]
        self.assertEqual({j["id"] for j in listed if j["id"].startswith("job-")}, {"job-chem"})
        self.assertEqual(self.call("POST", "/api/jobs/job-chem/stop", {}, agent="chem")[0], 200)

    def test_an_agent_cannot_answer_as_the_owner(self):
        d = self.call("POST", "/api/decisions", {"question": "ok?", "agent": "rat"}, agent="rat")[1]
        for agent in ("chem", "rat"):
            self.assertEqual(self.call("POST", f"/api/decisions/{d['id']}/answer", {"answer": "yes"}, agent=agent)[0], 409)
            self.assertEqual(self.call("POST", f"/api/inbox/{d['id']}/done", {"answer": "yes"}, agent=agent)[0], 409)
        self.assertEqual(self.h.s.decision(d["id"])["state"], "open")
        self.assertEqual(self.call("POST", f"/api/decisions/{d['id']}/answer", {"answer": "yes"})[1]["state"], "answered")

    def test_notify_reaches_the_owner_under_the_agents_own_name(self):
        status, res = self.call("POST", "/api/notifications", {"text": "need you", "agent": "chem"}, agent="chem")
        self.assertEqual(status, 200, res)
        self.assertEqual(self.call("POST", "/api/notifications", {"text": "x", "agent": "rat"}, agent="chem")[0], 409)

    def test_held_messages_stay_held(self):
        self.h.s.x("INSERT INTO messages(id,idem,to_agent,from_agent,body,state,attempts,created_at) "
                   "VALUES('m-held','m-held','chem','rat','x','held',0,'2026-01-01T00:00:00Z')")
        self.assertEqual(self.call("POST", "/api/messages/m-held/retry", {}, agent="chem")[0], 409)
        self.assertEqual(self.h.s.message("m-held")["state"], "held")
        self.assertEqual(self.call("POST", "/api/messages/m-held/cancel", {}, agent="rat")[0], 409)

    def test_an_agent_cannot_open_an_owner_shell(self):
        self.assertEqual(self.call("POST", "/api/agents", {"provider": "shell", "project": "labwork"}, agent="chem")[0], 409)

    def test_terminals_refuse_an_agent(self):
        out = self.shell('python3 -c "$CLIENT" "$@"', "GET", "/ws/agents/rat", "", env={"SB_AGENT": "chem"})
        self.assertTrue(out.startswith("403"), out)

    def test_fleet_shows_capacity_and_only_your_jobs(self):
        view = {"hosts": [{"name": "h", "capacity": {"threads": 8}, "top": [{"args": "secret"}], "units_running": ["u"],
                           "jobs": [{"unit": "sb-job-1.service"}, {"unit": "sb-job-2.service"}],
                           "tracked": [{"id": "1", "project": "labwork", "unit": "sb-job-1.service"},
                                       {"id": "2", "project": "rats", "unit": "sb-job-2.service"}],
                           "untracked": [{"pid": 1, "agent": "chem"}, {"pid": 2, "agent": "rat"}]}],
                "halt": {"by": "owner"}, "config": {"exists": True, "protected_units": ["p"]},
                "queued": [{"id": "q1", "project": "labwork"}, {"id": "q2", "project": "rats", "name": "theirs"}],
                "placers": {"workboard": {"jobs": [{"id": "a", "project": "labwork", "label": "mine"},
                                                   {"id": "b", "project": "rats", "label": "theirs"}]}}}
        v = barrier.fleet_for(view, "labwork", {"chem"})
        h = v["hosts"][0]
        self.assertEqual(h["capacity"]["threads"], 8)
        self.assertEqual([j["id"] for j in h["tracked"]], ["1"])
        self.assertEqual([j["id"] for j in v["queued"]], ["q1"])
        self.assertEqual([u["pid"] for u in h["untracked"]], [1])
        self.assertEqual(h["jobs"], [{"unit": "sb-job-1.service"}])  # a list: `sb fleet` takes its length
        self.assertNotIn("top", h)
        self.assertNotIn("units_running", h)
        self.assertEqual(v["halt"]["by"], "owner")
        self.assertNotIn("theirs", json.dumps(v))
        self.assertEqual(v["placers"]["workboard"]["jobs"][1]["project"], "(another project)")


if __name__ == "__main__":
    unittest.main()
