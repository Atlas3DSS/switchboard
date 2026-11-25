"""'Are you sure?': the hub looks at the jobs and files a claim names, tells the recipient what it found, and asks
the sender when something did not hold."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="sb-claims-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_SYSTEMD_USER_DIR"] = os.path.join(TMP, "units")
os.environ["SB_HOSTS"] = os.path.join(TMP, "hosts.json")
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))
from switchboard import claims  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.h = Hub(Store(os.path.join(self.dir, "hub.db")))
        self.h.s.upsert_project({"id": "alpha", "name": "Alpha", "match": {"cwd": [self.dir]}})
        for aid, prov in (("gem", "antigravity"), ("opus", "claude")):
            self.h.s.add_agent({"id": aid, "project": "alpha", "title": aid, "provider": prov, "account": "x",
                                "mode": "terminal", "cwd": self.dir, "state": "idle"})
        self.real = os.path.join(self.dir, "report.md")
        with open(self.real, "w") as f:
            f.write("results\n" * 30)

    def job(self, jid, state, script=None, exit_code=None):
        self.h.s.x("INSERT INTO jobs(id,name,agent,project,host,unit,cwd,threads,mem_mib,state,exit_code,argv,created_at)"
                   " VALUES(?,?,?,?,?,?,?,1,64,?,?,?,'2026-01-01T00:00:00Z')",
                   (jid, "n", "gem", "alpha", claims.hosts.load_config()["hosts"][0]["name"], f"sb-job-{jid}.service",
                    self.dir, state, exit_code, json.dumps(["python3", script] if script else ["true"])))

    def queued(self, aid):
        return [m["body"] for m in self.h.s.q("SELECT body FROM messages WHERE to_agent=? ORDER BY created_at", (aid,))]


class ClaimTests(Base):
    def setUp(self):
        super().setUp()
        claims.set_required(self.h.s, self.h.s.agent("gem"), False)  # the attest box has its own tests below

    def test_names_jobs_and_files_but_not_urls_routes_or_slash_commands(self):
        jobs, paths = claims.named("Launched job-1118193748-e31456. Wrote /mnt/x/out.json and ~/proj/res.md. See "
                                   "http://127.0.0.1:8802/api/overview, /usage, /api/work/3, "
                                   "/vr/cut, /vr/theater.js, /vr/prompt_engine.js, /findings/vision-senses, "
                                   "/home/.../gateway.py. Relative quotes `tests/test_a.py`, `src/coupon2.py`, "
                                   "`frame-vr.service`, `comfy-paths.yaml`, `83.4`, `Hub.send`, `.gcode.3mf`, `pine1..16`.",
                                   "/repo")
        self.assertEqual(jobs, ["job-1118193748-e31456"])
        self.assertEqual(paths, ["/mnt/x/out.json", os.path.expanduser("~/proj/res.md")])

    def test_skips_deleted_files_but_keeps_edited_and_kept_files(self):
        msg = ("Installed runtime into /opt/depth-pro-2. "
               "Removed leftover ~/.cache/frame-depth/comfy-paths.yaml. "
               "Deleted /tmp/old.yaml and fixed /tmp/server.py. "
               "/tmp/legacy.json was intentionally deleted. "
               "Read-only compliance: /tmp/coupon2.py was **not** created. "
               "Renamed /tmp/before.py to /tmp/after.py. "
               "Executing safely now: Welford pass over probe-*.npz -> /mnt/nvme2/salvage/moments.npz. "
               "Launching safely now: Phase 1 conversion into /mnt/nvme2/staging/. "
               "Will write summary to /tmp/future_summary.json. "
               "Removed dead ComfyUI code from /home/dev/app/gateway.py.")
        _, paths = claims.named(msg, "/repo")
        self.assertEqual(paths, ["/opt/depth-pro-2", "/tmp/server.py", "/tmp/after.py", "/home/dev/app/gateway.py"])

    def test_honest_withdrew_queued_and_failed_job_notices_hold(self):
        self.job("job-1111111111-aaaaaa", "stopped")
        self.job("job-2222222222-bbbbbb", "queued")
        self.job("job-3333333333-cccccc", "failed", exit_code=1)
        gem = self.h.s.agent("gem")
        # Honest notices hold
        self.assertTrue(claims.verify(self.h, gem, "Withdrew my queued gpu2 job (job-1111111111-aaaaaa).")[0]["ok"])
        self.assertTrue(claims.verify(self.h, gem, "Confirmed job-2222222222-bbbbbb queued cleanly behind it.")[0]["ok"])
        self.assertTrue(claims.verify(self.h, gem, "job-3333333333-cccccc failed with exit 1 (OOM).")[0]["ok"])
        # Negated 'not stopped' / 'not ended or withdrawn', imperative advice, and adjacent withdrawn job id
        self.assertTrue(claims.verify(self.h, gem, "job-2222222222-bbbbbb is still queued, not stopped.")[0]["ok"])
        self.assertTrue(claims.verify(self.h, gem, "Suggest you withdraw queued job-2222222222-bbbbbb once done.")[0]["ok"])
        pair = claims.verify(self.h, gem,
                             "Job job-2222222222-bbbbbb: Still queued (desk RAM 0 MiB), NOT ended or withdrawn "
                             "(job-1111111111-aaaaaa at 1.0G was the prior job withdrawn before resubmission).")
        self.assertEqual([(c["what"], c["ok"]) for c in pair],
                         [("job-2222222222-bbbbbb", True), ("job-1111111111-aaaaaa", True)])
        # False claims still fail
        self.assertFalse(claims.verify(self.h, gem, "job-2222222222-bbbbbb is now running on gpu2.")[0]["ok"])
        self.assertFalse(claims.verify(self.h, gem, "Active acceptance job job-3333333333-cccccc running.")[0]["ok"])
        self.assertFalse(claims.verify(self.h, gem, "I stopped job-2222222222-bbbbbb.")[0]["ok"])
        self.assertEqual([c["ok"] for c in claims.verify(
            self.h, gem, "I stopped job-1111111111-aaaaaa and job-2222222222-bbbbbb.")], [True, False])

    def test_verify_checks_host_named_in_message(self):
        cfg = {"hosts": [{"name": "desk", "ssh": None}, {"name": "gpubox", "ssh": "gpubox-ssh"}]}
        seen = []

        def fake_facts(host, path):
            seen.append((host["name"], path))
            if host["name"] == "gpubox" and path == "/opt/code-fb8":
                return {"exists": True, "dir": True, "size": 0, "lines": 0, "stub": 0}
            return {"exists": False}

        with mock.patch.object(claims.hosts, "load_config", return_value=cfg):
            res = claims.verify(self.h, self.h.s.agent("gem"), "Synced to Gpubox /opt/code-fb8", facts=fake_facts)
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["ok"])
        self.assertIn("gpubox", res[0]["why"])
        self.assertEqual(seen, [("desk", "/opt/code-fb8"), ("gpubox", "/opt/code-fb8")])

    def test_a_claim_that_holds_is_delivered_with_what_the_hub_saw(self):
        self.h.send({"to": "opus", "from": "gem", "body": f"Done. The report is written to {self.real}"})
        body = self.queued("opus")[0]
        self.assertIn("[hub-checked: " + self.real, body)
        self.assertEqual(self.queued("gem"), [])
        led = claims.ledger(self.h.s)["agents"][0]
        self.assertEqual((led["agent"], led["claims"], led["failed"], led["asked"]), ("gem", 1, 0, 0))

    def test_a_claim_that_does_not_hold_is_still_delivered_and_the_sender_is_asked(self):
        stub = os.path.join(self.dir, "run.py")
        with open(stub, "w") as f:
            f.write("# placeholder\nprint('PASS')\n")
        self.job("job-1111111111-aaaaaa", "running", stub)
        self.job("job-2222222222-bbbbbb", "failed", exit_code=1)
        missing = os.path.join(self.dir, "results", "final.json")
        self.h.send({"to": "opus", "from": "gem", "body": "Launched job-1111111111-aaaaaa and job-2222222222-bbbbbb, "
                     f"job-3333333333-cccccc is complete. Verified: results saved in {missing}"})
        body = self.queued("opus")[0]
        self.assertIn("Launched job-1111111111-aaaaaa", body)  # never held back, never rewritten
        self.assertIn("[hub-checked, NOT confirmed:", body)
        ask = self.queued("gem")[0]
        self.assertTrue(ask.startswith(claims.ASKED))
        for want in ("job-1111111111-aaaaaa: running", "is 2 lines and reads like a stub",
                     "job-2222222222-bbbbbb: failed, exit 1", "job-3333333333-cccccc: no such job",
                     missing + ": not found on"):
            self.assertIn(want, ask)
        led = claims.ledger(self.h.s)["agents"][0]
        self.assertEqual((led["failed"], led["asked"], led["things_failed"]), (1, 1, 4))

    def test_an_agent_that_is_not_watched_is_only_counted(self):
        self.h.send({"to": "gem", "from": "opus", "body": "done: wrote /tmp/nonexistent-dir-sb/x.txt"})
        self.assertEqual(self.queued("gem"), ["done: wrote /tmp/nonexistent-dir-sb/x.txt"])
        self.assertEqual(self.queued("opus"), [])
        self.assertEqual(claims.ledger(self.h.s)["agents"][0]["failed"], 1)

    def test_no_claim_or_nothing_named_is_left_alone(self):
        self.h.send({"to": "opus", "from": "gem", "body": f"Please read {self.real} and tell me what you think"})
        self.h.send({"to": "opus", "from": "gem", "body": "All done, everything verified."})
        self.assertEqual([b.count("hub-checked") for b in self.queued("opus")], [0, 0])
        self.assertEqual(claims.ledger(self.h.s)["agents"], [])

    def test_a_reply_at_rest_is_checked_once_and_not_asked_again_soon(self):
        t = mock.Mock(last_output=1.0)
        t.view.return_value, t.dialog.return_value, t.steady_for.return_value = (True, False, False), None, 60
        terms = mock.Mock(s=self.h.s, switching=set(), stuck={})
        gem = self.h.s.agent("gem")
        claims.check_reply(terms, gem, t, hub=self.h, reply="Finished. Output saved to /tmp/nonexistent-dir-sb/out.csv").join(5)
        self.assertEqual(len(self.queued("gem")), 1)
        t.last_output = 2.0  # it answers with another claim: no second question inside ASK_EVERY_S
        self.assertIsNone(claims.check_reply(terms, gem, t, hub=self.h, reply="Done, saved /tmp/nonexistent-dir-sb/b.csv"))
        self.assertIsNone(claims.check_reply(terms, self.h.s.agent("opus"), t, hub=self.h, reply="done /tmp/nonexistent-dir-sb/x"))
        self.assertEqual(len(self.queued("gem")), 1)


class AttestTests(Base):
    """The attest box: a ticked agent backs any 'done' with sb attest, and every request it gets says so."""

    def setUp(self):
        super().setUp()
        claims.set_required(self.h.s, self.h.s.agent("gem"), True)
        self.gem = self.h.s.agent("gem")

    def test_the_box_is_ticked_for_watched_agents_and_the_owner_can_change_it(self):
        self.assertTrue(claims.required(self.h.s, self.gem))
        self.assertFalse(claims.required(self.h.s, self.h.s.agent("opus")))
        self.assertTrue(claims.set_required(self.h.s, self.h.s.agent("opus"), True)["attest"])
        self.assertFalse(claims.set_required(self.h.s, self.gem, False)["attest"])
        self.assertTrue(self.h.agent_view(self.h.s.agent("opus"))["attest"])

    def test_attest_checks_the_evidence_and_gives_a_receipt_or_asks(self):
        r = claims.attest(self.h, self.gem, {"claim": "the report is written", "files": ["report.md"],
                                             "quotes": [["report.md", "results"]]})
        self.assertTrue(r["ok"])
        self.assertRegex(r["receipt"], r"^att-[0-9a-f]{8}$")
        self.assertIn("Are you sure this is everything the request asked for?", r["say"])
        bad = claims.attest(self.h, self.gem, {"claim": "sweep finished, accuracy 0.93", "jobs": ["job-9999999999-ffffff"],
                                               "files": ["nope.json"], "quotes": [["report.md", "accuracy 0.93"]]})
        self.assertFalse(bad["ok"])
        self.assertIsNone(bad["receipt"])
        for want in (claims.ASKED, "job-9999999999-ffffff: no such job", "nope.json: not found on",
                     "contains 'accuracy 0.93': that text is not in the file", "No receipt"):
            self.assertIn(want, bad["say"])
        none = claims.attest(self.h, self.gem, {"claim": "everything works"})
        self.assertIn("You named nothing that can be checked", none["say"])
        with self.assertRaises(ValueError):
            claims.attest(self.h, self.gem, {"files": ["report.md"]})
        led = claims.ledger(self.h.s)["agents"][0]
        self.assertEqual((led["attests"], led["attests_failed"]), (3, 2))

    def test_reporting_a_job_as_stopped_is_a_claim_that_can_hold(self):
        self.job("job-4444444444-dddddd", "stopped")
        self.job("job-5555555555-eeeeee", "running")
        ok = claims.attest(self.h, self.gem, {"claim": "job-4444444444-dddddd was stopped by gem", "jobs": ["job-4444444444-dddddd"]})
        self.assertTrue(ok["ok"], ok["say"])
        self.assertFalse(claims.attest(self.h, self.gem, {"claim": "the sweep is launched", "jobs": ["job-4444444444-dddddd"]})["ok"])
        still = claims.attest(self.h, self.gem, {"claim": "I stopped the duplicate", "jobs": ["job-5555555555-eeeeee"]})
        self.assertIn("is still running, not stopped", still["say"])
        mixed = claims.verify(self.h, self.gem, "Launched job-4444444444-dddddd on gpubox. " + "x " * 80
                              + "Separately I stopped job-5555555555-eeeeee.")
        self.assertEqual([c["ok"] for c in mixed], [False, False])  # each id is read with the words around it

    def test_every_request_to_a_ticked_agent_says_a_receipt_is_expected(self):
        self.h.send({"to": "gem", "from": "opus", "body": "please write the report"})
        self.h.send({"to": "opus", "from": "gem", "body": "ok, starting"})
        self.assertTrue(self.queued("gem")[0].endswith(claims.FOOTER))
        self.assertEqual(self.queued("opus"), ["ok, starting"])

    def test_a_report_with_no_receipt_is_delivered_marked_and_sent_back(self):
        self.h.send({"to": "opus", "from": "gem", "body": "All done, everything verified."})
        self.assertIn("carries no attest receipt", self.queued("opus")[0])
        self.assertTrue(self.queued("gem")[0].startswith(claims.ASKED))
        self.assertIn("sb attest --claim", self.queued("gem")[0])

    def test_a_report_with_its_own_receipt_goes_through_marked_attested(self):
        rid = claims.attest(self.h, self.gem, {"claim": "report written", "files": [self.real]})["receipt"]
        self.h.send({"to": "opus", "from": "gem", "body": f"Done: report written to {self.real}. Receipt {rid}"})
        self.assertIn(f"[hub: attested, receipt {rid}]", self.queued("opus")[0])
        self.assertEqual(self.queued("gem"), [])
        other = claims.attest(self.h, {**self.h.s.agent("opus")}, {"claim": "x", "files": [self.real]})["receipt"]
        self.h.send({"to": "opus", "from": "gem", "body": f"Finished the second part too. Receipt {other}"})
        self.assertEqual(len(self.queued("gem")), 1)  # another agent's receipt does not count

    def test_a_final_reply_with_no_receipt_is_asked(self):
        t = mock.Mock(last_output=1.0)
        t.view.return_value, t.dialog.return_value, t.steady_for.return_value = (True, False, False), None, 60
        terms = mock.Mock(s=self.h.s, switching=set(), stuck={})
        claims.check_reply(terms, self.gem, t, hub=self.h, reply="I have fixed the card and verified it.").join(5)
        self.assertIn("carries no attest receipt", self.queued("gem")[0])


if __name__ == "__main__":
    unittest.main()
