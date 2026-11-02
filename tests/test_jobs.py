"""Jobs (docs/FLEET.md phase 2): placement from the inventory with reserves and running jobs subtracted and the
display GPU avoided; a real launch on this machine as a transient unit in the jobs slice, tracked to finished,
failed (exit code) and stopped; HALT refuses submission and leaves queued jobs queued; a queued job carries the
reason; adopt watches an existing unit and sees it leave; untracked work is attributed to the agent terminal it
descends from; the agent note names sb run.
Run: python3 -m unittest tests.test_jobs
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbjobs-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_SYSTEMD_USER_DIR"] = os.path.join(TMP, "units")
os.environ["SB_SSH"] = os.path.join(HERE, "fake_ssh.py")
os.environ["SB_HOSTS"] = os.path.join(TMP, "hosts.json")
os.environ["SB_FLEET_PROBE_S"] = "2"
os.environ.setdefault("SB_CONFIG", os.path.join(TMP, "accounts.json"))
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))
os.environ.setdefault("SB_CLAUDE_BIN", os.path.join(HERE, "fake_cli.py"))
os.environ.setdefault("SB_CODEX_BIN", os.path.join(HERE, "fake_cli.py"))

from switchboard import hosts, jobs, shared  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402

os.makedirs(os.environ["SB_SYSTEMD_USER_DIR"], exist_ok=True)


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


def wait_for(fn, timeout=30.0, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def fake_row(name, threads=32, cores=16, load=0.0, avail_mib=100 * 1024, gpus=(), ok=True):
    doc = {"identity": {"time": time.time()}, "cpu": {"threads": threads, "cores": cores, "load": [load, 0, 0]},
           "memory": {"total_mib": 128 * 1024, "available_mib": avail_mib}, "gpus": list(gpus), "units": [], "jobs": []}
    return {"name": name, "doc": json.dumps(doc), "ok": 1 if ok else 0, "error": None if ok else "down",
            "probed_at": "2026-01-01T00:00:00Z"}


def gpu(uuid_, free, display=False):
    return {"uuid": uuid_, "name": "card", "vram_total_mib": free + 1000, "vram_used_mib": 1000, "display": display,
            "util": 0, "procs": []}


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()

    def job(self, **kw):
        base = {"id": "job-x", "threads": 1, "mem_mib": 1024, "gpu_mib": None, "host_pref": None}
        return {**base, **kw}

    def cfg(self, names, **jobs_cfg):
        write_hosts([{"name": n, "ssh": None if i == 0 else n, "order": i + 1} for i, n in enumerate(names)],
                    jobs=jobs_cfg)
        return hosts.load_config()

    def test_order_then_fit(self):
        cfg = self.cfg(["first", "second"])
        rows = {"first": fake_row("first", threads=4, cores=4, load=3.0), "second": fake_row("second")}
        h, g, why = jobs.place(self.h, cfg, self.job(threads=2), rows)
        self.assertEqual(h["name"], "second")  # first has 4 - 1 reserve - 3 load = 0 free
        h, g, why = jobs.place(self.h, cfg, self.job(threads=1, mem_mib=512), rows)
        self.assertEqual(h["name"], "second")
        h, g, why = jobs.place(self.h, cfg, self.job(threads=200), rows)
        self.assertIsNone(h)
        self.assertIn("threads free, need 200", why)

    def test_unreachable_and_memory(self):
        cfg = self.cfg(["a", "b"])
        rows = {"a": fake_row("a", ok=False), "b": fake_row("b", avail_mib=9 * 1024)}  # 9 GiB - 8 reserve = 1 GiB
        h, g, why = jobs.place(self.h, cfg, self.job(mem_mib=2048), rows)
        self.assertIsNone(h)
        self.assertIn("a: unreachable", why)
        self.assertIn("b: 1024 MiB free, need 2048", why)

    def test_display_gpu_avoided_then_used_as_fallback_never_under_never(self):
        cfg = self.cfg(["desk", "box"], display_gpu="avoid")
        rows = {"desk": fake_row("desk", gpus=[gpu("D", 90000, display=True)]),
                "box": fake_row("box", gpus=[gpu("B", 30000)])}
        h, g, why = jobs.place(self.h, cfg, self.job(gpu_mib=20000), rows)
        self.assertEqual((h["name"], g["uuid"]), ("box", "B"))
        h, g, why = jobs.place(self.h, cfg, self.job(gpu_mib=50000), rows)
        self.assertEqual((h["name"], g["uuid"]), ("desk", "D"), "only the display card fits: used as fallback")
        cfg = self.cfg(["desk", "box"], display_gpu="never")
        h, g, why = jobs.place(self.h, cfg, self.job(gpu_mib=50000), rows)
        self.assertIsNone(h)
        self.assertIn("no GPU with 50000 MiB free", why)

    def test_running_jobs_reserve_capacity(self):
        cfg = self.cfg(["only"])
        rows = {"only": fake_row("only", threads=8, cores=8, gpus=[gpu("G", 10000)])}
        self.h.s.x("INSERT INTO jobs(id,name,host,unit,threads,mem_mib,gpu_mib,gpu_uuid,state,created_at) "
                   "VALUES('job-r','r','only','sb-job-job-r.service',6,1024,8000,'G','running','2026-01-01T00:00:00Z')")
        h, g, why = jobs.place(self.h, cfg, self.job(threads=2), rows)
        self.assertIsNone(h, why)  # 8 - 1 reserve - 6 held = 1 free
        h, g, why = jobs.place(self.h, cfg, self.job(threads=1, gpu_mib=4000), rows)
        self.assertIsNone(h)  # 10000 - 8000 held < 4000
        h, g, why = jobs.place(self.h, cfg, self.job(threads=1, gpu_mib=1500), rows)
        self.assertEqual(g["uuid"], "G")

    def test_host_pref_limits_candidates(self):
        cfg = self.cfg(["a", "b"])
        rows = {"a": fake_row("a"), "b": fake_row("b")}
        h, g, why = jobs.place(self.h, cfg, self.job(host_pref="b"), rows)
        self.assertEqual(h["name"], "b")


class SubmitValidationTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        write_hosts([{"name": "here", "ssh": None}])

    def test_bad_inputs(self):
        for b in ({"name": "bad name", "argv": ["true"]}, {"name": "ok", "argv": []}, {"name": "ok", "argv": ["true"], "threads": 0},
                  {"name": "ok", "argv": ["true"], "cwd": "rel/path"}):
            with self.assertRaises(ValueError, msg=b):
                jobs.submit(self.h, b)
        with self.assertRaises(LookupError):
            jobs.submit(self.h, {"name": "ok", "argv": ["true"], "host": "nowhere"})

    def test_halt_refuses_and_queued_stays_queued(self):
        hosts.halt(self.h, "tester", "test", None, False)
        try:
            with self.assertRaises(ValueError) as cm:
                jobs.submit(self.h, {"name": "ok", "argv": ["true"]})
            self.assertIn("HALT raised by tester", str(cm.exception))
            self.h.s.x("INSERT INTO jobs(id,name,host,unit,cwd,argv,threads,mem_mib,hours,state,created_at) VALUES"
                       "('job-q','q',NULL,'sb-job-job-q.service','/tmp','[\"true\"]',1,64,1,'queued','2026-01-01T00:00:00Z')")
            self.assertEqual(jobs.launch_queued(self.h), [])
            self.assertEqual(jobs._row(self.h, "job-q")["state"], "queued")
        finally:
            hosts.resume(self.h, "tester")

    def test_nothing_fits_means_queued_with_reason(self):
        hosts.tick(self.h)
        j = jobs.submit(self.h, {"name": "huge", "argv": ["true"], "threads": 4000, "agent": "a1"})
        self.assertEqual(j["state"], "queued")
        self.assertIn("threads free, need 4000", j["reason"])
        self.assertEqual(j["project"], "alpha", "project comes from the agent")
        j = jobs.stop(self.h, j["id"], "a1")
        self.assertEqual(j["state"], "stopped")
        self.assertIn("before it was placed", j["reason"])


@unittest.skipUnless(have_user_systemd(), "needs a systemd user manager")
class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        shutil.rmtree(os.environ["SB_STATE"], ignore_errors=True)
        write_hosts([{"name": "here", "ssh": None, "desktop": True, "reserve": {"cores": 1, "ram_gib": 1}}])
        hosts.tick(self.h)

    def tearDown(self):
        for r in self.h.s.q("SELECT unit FROM jobs"):
            subprocess.run(["systemctl", "--user", "stop", r["unit"]], capture_output=True)
            subprocess.run(["systemctl", "--user", "reset-failed", r["unit"]], capture_output=True)

    def settle(self, jid, states, timeout=40):
        def step():
            hosts.tick(self.h)
            jobs.tick(self.h)
            j = jobs._row(self.h, jid)
            return j if j["state"] in states else None
        return wait_for(step, timeout, 1.0)

    def unit_props(self, unit, props):
        out = subprocess.run(["systemctl", "--user", "show", "-p", ",".join(props), "--", unit], capture_output=True,
                             text=True).stdout
        return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)

    def test_run_track_finish(self):
        j = jobs.submit(self.h, {"name": "ok", "argv": ["bash", "-c", "echo hello; sleep 2"], "threads": 1,
                                 "mem_mib": 256, "hours": 0.1, "cwd": "/tmp", "agent": "a1"})
        self.assertEqual(j["state"], "running", j["reason"])
        self.assertEqual(j["host"], "here")
        p = self.unit_props(j["unit"], ["Slice", "CPUWeight", "MemoryMax", "RuntimeMaxUSec", "WorkingDirectory", "Environment"])
        self.assertEqual(p["Slice"], "switchboard-jobs.slice")
        self.assertEqual(p["CPUWeight"], "20")
        self.assertEqual(p["MemoryMax"], str(256 * 1024 * 1024))
        self.assertEqual(p["RuntimeMaxUSec"], "6min")
        self.assertIn(f"SB_JOB_ID={j['id']}", p["Environment"])
        self.assertIn("SB_AGENT=a1", p["Environment"])
        slice_file = os.path.join(os.environ["SB_SYSTEMD_USER_DIR"], "switchboard-jobs.slice")
        self.assertIn("CPUWeight=20", open(slice_file).read())
        done = self.settle(j["id"], ("finished", "failed", "lost"))
        self.assertEqual(done["state"], "finished", done["reason"])
        self.assertEqual(done["exit_code"], 0)
        self.assertIsNotNone(done["finished_at"])
        v = jobs.show(self.h, j["id"])
        self.assertIn("hello", v["log"])
        self.assertEqual(self.unit_props(j["unit"], ["LoadState"])["LoadState"], "not-found", "collected")
        self.assertEqual(jobs.list_jobs(self.h)["jobs"], [])
        self.assertEqual(jobs.list_jobs(self.h, include_all=True)["jobs"][0]["id"], j["id"])

    def test_exit_code_is_failed(self):
        j = jobs.submit(self.h, {"name": "bad", "argv": ["bash", "-c", "exit 3"], "mem_mib": 128, "hours": 0.1,
                                 "cwd": "/tmp"})
        done = self.settle(j["id"], ("finished", "failed", "lost"))
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["exit_code"], 3)
        self.assertIn("exit 3", done["reason"])

    def test_stop_running_job(self):
        j = jobs.submit(self.h, {"name": "long", "argv": ["sleep", "300"], "mem_mib": 128, "hours": 1, "cwd": "/tmp"})
        self.assertEqual(j["state"], "running")
        running = self.settle(j["id"], ("running",))
        self.assertTrue(running["pid"])
        s = jobs.stop(self.h, j["id"], "a1", "test over")
        self.assertEqual(s["state"], "stopped")
        self.assertIn("stopped by a1", s["reason"])
        self.assertEqual(self.unit_props(j["unit"], ["ActiveState"])["ActiveState"], "inactive")

    def test_launch_refused_when_halt_appears_on_the_host(self):
        # the flag appears on the host between placement and start (here: raised directly on the file system)
        os.makedirs(os.environ["SB_STATE"], exist_ok=True)
        self.h.s.x("INSERT INTO jobs(id,name,host,unit,cwd,argv,threads,mem_mib,hours,state,created_at,host_pref) VALUES"
                   "('job-h','h',NULL,'sb-job-job-h.service','/tmp','[\"true\"]',1,64,1,'queued','2026-01-01T00:00:00Z',NULL)")
        with open(os.path.join(os.environ["SB_STATE"], "HALT"), "w") as f:
            f.write("test\n")
        try:
            jobs.launch_queued(self.h)  # hub setting is clear, the host file is not
            self.assertEqual(jobs._row(self.h, "job-h")["state"], "refused")
        finally:
            os.remove(os.path.join(os.environ["SB_STATE"], "HALT"))

    def test_adopt_watches_and_sees_the_unit_leave(self):
        unit = f"sbtest-adopt-{uuid.uuid4().hex[:8]}"
        subprocess.run(["systemd-run", "--user", "--quiet", "--unit", unit, "sleep", "300"], check=True)
        try:
            j = jobs.adopt(self.h, {"unit": unit, "name": "legacy", "host": "here", "agent": "a1"})
            self.assertEqual(j["state"], "running")
            self.assertTrue(j["adopted"])
            with self.assertRaises(ValueError):
                jobs.adopt(self.h, {"unit": unit, "name": "legacy2", "host": "here"})
            res = hosts.halt(self.h, "tester", "test", ["here"], False)
            self.assertEqual(res["results"][0]["stopped"], [], "adopted units are never halted")
            hosts.resume(self.h, "tester")
            subprocess.run(["systemctl", "--user", "stop", unit], check=True)
            done = self.settle(j["id"], ("finished",))
            self.assertEqual(done["reason"], "unit left")
        finally:
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)

    def test_fleet_view_lists_tracked_jobs(self):
        j = jobs.submit(self.h, {"name": "seen", "argv": ["sleep", "60"], "mem_mib": 128, "hours": 1, "cwd": "/tmp"})
        v = hosts.fleet_view(self.h)
        self.assertEqual([t["id"] for t in v["hosts"][0]["tracked"]], [j["id"]])
        self.assertEqual(hosts.summary(self.h)["hosts"][0]["jobs"], 1)
        jobs.stop(self.h, j["id"], "a1")


class AttributionTests(unittest.TestCase):
    def test_untracked_names_the_agent_terminal(self):
        cfg = {"protected_units": [], "workload_units": []}
        doc = {"processes": [
            {"pid": 50, "cpu": 80.0, "comm": "python3", "unit": None, "cwd": "/w", "ancestors": [40, 30, 20, 1]},
            {"pid": 30, "cpu": 60.0, "comm": "claude", "unit": None, "cwd": "/w", "ancestors": [20, 1]},
            {"pid": 70, "cpu": 70.0, "comm": "python3", "unit": "rosa.service", "cwd": "/w", "ancestors": [1]}]}
        u = hosts.untracked(cfg, doc, agent_pids={30: "a1"})
        self.assertEqual([(x["pid"], x["attribution"], x["agent"]) for x in u],
                         [(50, "agent a1 (terminal)", "a1"), (70, "rosa.service", None)])

    def test_agent_note_names_sb_run(self):
        note = shared.launch_note({"id": "a1", "project": "alpha"})
        self.assertIn("sb run --name NAME", note)
        self.assertIn("untracked", note)


if __name__ == "__main__":
    unittest.main()
