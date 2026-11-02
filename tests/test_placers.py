"""Placement backends (docs/FLEET.md phase 3), with fake tools: a GPU job goes submit -> acquire -> launch with the
card the board assigned -> start -> heartbeat -> finish; a board refusal leaves it queued with the board's reason
and retries without resubmitting; a board preemption becomes `preempted`; cpu-run's advice pins the unit's CPUs
and a refusal falls back to the hub's own placement; project budgets queue the job over the cap; the fleet view
shows the board. Run: python3 -m unittest tests.test_placers
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbplacers-")
os.environ.setdefault("SB_STATE", os.path.join(TMP, "state"))
os.environ["SB_SYSTEMD_USER_DIR"] = os.path.join(TMP, "units")
os.environ["SB_SSH"] = os.path.join(HERE, "fake_ssh.py")
os.environ.setdefault("SB_HOSTS", os.path.join(TMP, "hosts.json"))
os.environ.setdefault("SB_CONFIG", os.path.join(TMP, "accounts.json"))
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))
os.environ.setdefault("SB_CLAUDE_BIN", os.path.join(HERE, "fake_cli.py"))
os.environ.setdefault("SB_CODEX_BIN", os.path.join(HERE, "fake_cli.py"))

from switchboard import hosts, jobs, placers  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402

os.makedirs(os.environ["SB_SYSTEMD_USER_DIR"], exist_ok=True)
FAKE_WB = os.path.join(HERE, "fake_workboard.py")
FAKE_CPU = os.path.join(HERE, "fake_cpu_run.py")


def have_user_systemd():
    try:
        return subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True,
                              timeout=5).stdout.strip() in ("running", "degraded")
    except (OSError, subprocess.SubprocessError):
        return False


def write_hosts(hosts_list, **extra):
    with open(os.environ["SB_HOSTS"], "w") as f:
        json.dump({"hosts": hosts_list, **extra}, f)


def new_hub():
    h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
    h.s.upsert_project({"id": "alpha", "name": "Alpha"})
    h.s.add_agent({"id": "a1", "project": "alpha", "title": "a1", "provider": "claude", "account": "one",
                   "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
    return h


def fabricate_here(hub, gpus=()):
    """A probe document for this machine with the GPUs we say, so GPU placement works without a card."""
    doc, err, ms = hosts.probe({"name": "here", "ssh": None})
    doc["gpus"] = list(gpus)
    hub.s.x("INSERT INTO hosts(name,cfg,doc,probed_at,ok,error,ms) VALUES('here',?,?,?,1,NULL,1)",
            (json.dumps({"name": "here", "ssh": None, "desktop": True, "order": 1,
                         "reserve": {"cores": 1, "ram_gib": 1}, "labels": []}), json.dumps(doc), hosts.now()))


def wb_calls(log):
    return [json.loads(l) for l in open(log)] if os.path.exists(log) else []


def unit_props(unit, props):
    out = subprocess.run(["systemctl", "--user", "show", "-p", ",".join(props), "--", unit], capture_output=True,
                         text=True).stdout
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


class AvailabilityTests(unittest.TestCase):
    def test_disabled_and_explicit_paths(self):
        self.assertEqual(placers.available({"jobs": {"gpu_workboard": None, "cpu_run": "none"}}),
                         {"gpu_workboard": None, "cpu_run": None})
        a = placers.available({"jobs": {"gpu_workboard": FAKE_WB, "cpu_run": FAKE_CPU}})
        self.assertEqual(a, {"gpu_workboard": FAKE_WB, "cpu_run": FAKE_CPU})
        self.assertIsNone(placers.available({"jobs": {"gpu_workboard": "/no/such/tool", "cpu_run": None}})["gpu_workboard"])

    def test_status_none_when_disabled(self):
        write_hosts([{"name": "here", "ssh": None}], jobs={"gpu_workboard": None, "cpu_run": None})
        self.assertIsNone(placers.wb_status(hosts.load_config()))


class BudgetTests(unittest.TestCase):
    def test_budget_queues_over_cap(self):
        h = new_hub()
        write_hosts([{"name": "here", "ssh": None}], budgets={"alpha": {"jobs": 1, "threads": 4}},
                    jobs={"gpu_workboard": None, "cpu_run": None})
        cfg = hosts.load_config()
        h.s.x("INSERT INTO jobs(id,name,project,host,unit,threads,mem_mib,state,created_at) VALUES"
              "('job-a','a','alpha','here','sb-job-job-a.service',2,512,'running','2026-01-01T00:00:00Z')")
        job = {"id": "job-b", "project": "alpha", "threads": 1, "mem_mib": 256, "gpu_mib": None, "host_pref": None}
        self.assertIn("budget: jobs 1 used + 1 asked > 1", jobs.budget_check(h, cfg, job))
        h.s.x("UPDATE jobs SET state='finished' WHERE id='job-a'")
        self.assertIsNone(jobs.budget_check(h, cfg, job))
        job["threads"] = 5
        self.assertIn("threads 0 used + 5 asked > 4", jobs.budget_check(h, cfg, job))
        self.assertIsNone(jobs.budget_check(h, cfg, {**job, "project": "beta"}), "no budget for beta, no default")


@unittest.skipUnless(have_user_systemd(), "needs a systemd user manager")
class WorkboardTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        self.log = os.path.join(tempfile.mkdtemp(dir=TMP), "wb.jsonl")
        os.environ["FAKE_WB_LOG"] = self.log
        for k in ("FAKE_WB_DENY", "FAKE_WB_STATUS", "FAKE_WB_REASON"):
            os.environ.pop(k, None)
        write_hosts([{"name": "here", "ssh": None, "desktop": True, "reserve": {"cores": 1, "ram_gib": 1}}],
                    jobs={"gpu_workboard": FAKE_WB, "cpu_run": None, "display_gpu": "avoid"})
        fabricate_here(self.h, gpus=[{"uuid": "GPU-DOC", "name": "doc-card", "vram_total_mib": 16000,
                                      "vram_used_mib": 1000, "display": False, "util": 0, "procs": []}])

    def tearDown(self):
        for r in self.h.s.q("SELECT unit FROM jobs"):
            subprocess.run(["systemctl", "--user", "stop", r["unit"]], capture_output=True)
            subprocess.run(["systemctl", "--user", "reset-failed", r["unit"]], capture_output=True)

    def settle(self, jid, states, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            hosts.tick(self.h)
            jobs.tick(self.h)
            j = jobs._row(self.h, jid)
            if j["state"] in states:
                return j
            time.sleep(1)
        return jobs._row(self.h, jid)

    def test_lease_lifecycle(self):
        j = jobs.submit(self.h, {"name": "gpu-ok", "argv": ["sleep", "60"], "gpu_mib": 2048, "mem_mib": 128, "hours": 1,
                                 "cwd": "/tmp", "agent": "a1", "protected": True})
        self.assertEqual(j["state"], "running", j["reason"])
        self.assertEqual(j["board_id"], "gpu-fake-1")
        self.assertEqual(j["placer"], "gpu-workboard")
        self.assertEqual(j["gpu_uuid"], "GPU-FAKE-0001", "the card the board assigned, not the one we picked")
        calls = wb_calls(self.log)
        self.assertEqual([c[0] for c in calls], ["submit", "acquire", "start"])
        self.assertIn("--protected", calls[0])
        self.assertIn("alpha", calls[0])
        self.assertEqual(calls[0][calls[0].index("--gpu") + 1], "GPU-DOC")
        self.assertIn("--unit", calls[2])
        self.assertIn("CUDA_VISIBLE_DEVICES=GPU-FAKE-0001", unit_props(j["unit"], ["Environment"])["Environment"])
        jobs.tick(self.h)
        self.assertIn("heartbeat", [c[0] for c in wb_calls(self.log)])
        jobs.stop(self.h, j["id"], "a1")
        jobs.collect(self.h)
        fin = [c for c in wb_calls(self.log) if c[0] == "finish"]
        self.assertEqual(len(fin), 1)
        self.assertEqual(fin[0][fin[0].index("--outcome") + 1], "released")

    def test_board_refusal_queues_and_retries_without_resubmitting(self):
        os.environ["FAKE_WB_DENY"] = "card lacks room"
        j = jobs.submit(self.h, {"name": "gpu-wait", "argv": ["sleep", "60"], "gpu_mib": 2048, "mem_mib": 128, "hours": 1,
                                 "cwd": "/tmp"})
        self.assertEqual(j["state"], "queued")
        self.assertEqual(j["reason"], "GPU workboard: card lacks room")
        self.assertEqual(j["board_id"], "gpu-fake-1")
        jobs.launch_queued(self.h)
        self.assertEqual([c[0] for c in wb_calls(self.log)], ["submit", "acquire", "acquire"], "no second submit")
        del os.environ["FAKE_WB_DENY"]
        jobs.launch_queued(self.h)
        self.assertEqual(jobs._row(self.h, j["id"])["state"], "running")
        jobs.stop(self.h, j["id"], "a1")

    def test_preemption_is_its_own_state(self):
        j = jobs.submit(self.h, {"name": "gpu-pre", "argv": ["sleep", "100"], "gpu_mib": 2048, "mem_mib": 128, "hours": 1,
                                 "cwd": "/tmp"})
        self.assertEqual(j["state"], "running", j["reason"])
        subprocess.run(["systemctl", "--user", "kill", "-s", "KILL", j["unit"]], check=True)
        os.environ["FAKE_WB_STATUS"] = "preempted"
        os.environ["FAKE_WB_REASON"] = "interactive request needed the card"
        done = self.settle(j["id"], ("preempted", "failed", "lost"))
        self.assertEqual(done["state"], "preempted", done["reason"])
        self.assertIn("checkpoint preemption", done["reason"])
        jobs.collect(self.h)
        self.assertEqual([c for c in wb_calls(self.log) if c[0] == "finish"], [], "the board already owns a preempted job")

    def test_fleet_view_shows_the_board(self):
        v = hosts.fleet_view(self.h)
        self.assertTrue(v["placers"]["gpu_workboard"])
        self.assertEqual(v["placers"]["workboard"]["cards"][0]["label"], "fake-card")


@unittest.skipUnless(have_user_systemd(), "needs a systemd user manager")
class CpuRunTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        for k in ("FAKE_CPU_REFUSE", "FAKE_CPU_HOST", "FAKE_CPU_CPUS"):
            os.environ.pop(k, None)
        os.environ["FAKE_CPU_LOG"] = os.path.join(tempfile.mkdtemp(dir=TMP), "cpu.jsonl")
        write_hosts([{"name": "here", "ssh": None, "desktop": True, "reserve": {"cores": 1, "ram_gib": 1},
                      "cpu_run_name": "devbox"}],
                    jobs={"gpu_workboard": None, "cpu_run": FAKE_CPU})
        hosts.tick(self.h)

    def tearDown(self):
        for r in self.h.s.q("SELECT unit FROM jobs"):
            subprocess.run(["systemctl", "--user", "stop", r["unit"]], capture_output=True)
            subprocess.run(["systemctl", "--user", "reset-failed", r["unit"]], capture_output=True)

    def test_advice_pins_cpus(self):
        os.environ["FAKE_CPU_HOST"], os.environ["FAKE_CPU_CPUS"] = "devbox", "0,1"
        j = jobs.submit(self.h, {"name": "cpu-pin", "argv": ["sleep", "60"], "mem_mib": 128, "hours": 1, "cwd": "/tmp"})
        self.assertEqual(j["state"], "running", j["reason"])
        self.assertEqual(j["placer"], "cpu-run")
        self.assertEqual(j["cpus"], [0, 1])
        self.assertEqual(unit_props(j["unit"], ["AllowedCPUs"])["AllowedCPUs"], "0-1")
        argv = json.loads(open(os.environ["FAKE_CPU_LOG"]).read().splitlines()[0])
        self.assertIn("--dry-run", argv)
        self.assertEqual(argv[argv.index("--threads") + 1], "1")
        jobs.stop(self.h, j["id"], "a1")

    def test_refusal_and_unknown_host_fall_back_to_the_hub(self):
        os.environ["FAKE_CPU_REFUSE"] = "insufficient idle CPU capacity"
        j = jobs.submit(self.h, {"name": "cpu-fb", "argv": ["sleep", "60"], "mem_mib": 128, "hours": 1, "cwd": "/tmp"})
        self.assertEqual(j["state"], "running", j["reason"])
        self.assertIn("hub (cpu-run: here: insufficient idle CPU capacity", j["placer"])
        self.assertIsNone(j["cpus"])
        self.assertEqual(unit_props(j["unit"], ["AllowedCPUs"])["AllowedCPUs"], "")
        jobs.stop(self.h, j["id"], "a1")
        del os.environ["FAKE_CPU_REFUSE"]
        os.environ["FAKE_CPU_HOST"] = "elsewhere"
        j = jobs.submit(self.h, {"name": "cpu-fb2", "argv": ["sleep", "60"], "mem_mib": 128, "hours": 1, "cwd": "/tmp"})
        self.assertEqual(j["state"], "running", j["reason"])
        self.assertIn("not in hosts.json", j["placer"])
        jobs.stop(self.h, j["id"], "a1")


if __name__ == "__main__":
    unittest.main()
