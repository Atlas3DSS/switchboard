"""Fleet inventory and halt (docs/FLEET.md phase 1): the probe answers on this machine; a hosts file with a remote
host reached through a fake ssh (runs locally, no network); an unreachable host keeps its last document and is
marked down; capacity arithmetic; HALT raised and lowered on every host (into a temp SB_STATE, never the real
one); tracked sb-job units stopped by a halt; guards installed only on workload units that are not protected.
Run: python3 -m unittest tests.test_hosts
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbhosts-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")           # HALT lands here, on the "remote" host too
os.environ["SB_SYSTEMD_USER_DIR"] = os.path.join(TMP, "units")  # guards land here
os.environ["SB_SSH"] = os.path.join(HERE, "fake_ssh.py")
os.environ["SB_HOSTS"] = os.path.join(TMP, "hosts.json")
os.environ.setdefault("SB_CONFIG", os.path.join(TMP, "accounts.json"))
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))
os.environ.setdefault("SB_CLAUDE_BIN", os.path.join(HERE, "fake_cli.py"))
os.environ.setdefault("SB_CODEX_BIN", os.path.join(HERE, "fake_cli.py"))

from switchboard import hosts  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402

os.environ["SB_HOSTS"] = hosts.CONFIG  # the path the module captured at import: shared when suites run together
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
    return Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))


class ProbeTests(unittest.TestCase):
    def test_local_probe_answers(self):
        doc, err, ms = hosts.probe({"name": "me", "ssh": None})
        self.assertIsNone(err, err)
        self.assertEqual(doc["cpu"]["threads"], os.cpu_count())
        self.assertGreater(doc["memory"]["total_mib"], 0)
        self.assertEqual(doc["identity"]["hostname"].split(".")[0], os.uname().nodename.split(".")[0])
        self.assertIsInstance(doc["processes"], list)
        self.assertIsInstance(doc["gpus"], list)
        self.assertEqual(doc["errors"], [])

    def test_probe_over_fake_ssh_is_the_same_machine(self):
        doc, err, ms = hosts.probe({"name": "far", "ssh": "anything"})
        self.assertIsNone(err, err)
        self.assertEqual(doc["identity"]["boot_id"], hosts.probe({"name": "me", "ssh": None})[0]["identity"]["boot_id"])

    def test_unreachable_host_reports_the_ssh_error(self):
        doc, err, ms = hosts.probe({"name": "gone", "ssh": "unreachable"})
        self.assertIsNone(doc)
        self.assertIn("No route to host", err)


class ConfigTests(unittest.TestCase):
    def test_missing_file_means_this_machine_only(self):
        if os.path.exists(os.environ["SB_HOSTS"]):
            os.remove(os.environ["SB_HOSTS"])
        cfg = hosts.load_config()
        self.assertFalse(cfg["exists"])
        self.assertEqual(len(cfg["hosts"]), 1)
        self.assertIsNone(cfg["hosts"][0]["ssh"])
        self.assertEqual(cfg["jobs"]["display_gpu"], "avoid")

    def test_file_adds_hosts_and_keeps_a_local_one(self):
        write_hosts([{"name": "box", "ssh": "box", "order": 1}, {"name": "desk", "ssh": None, "desktop": True, "order": 3}],
                    workload_units=["atlas-*"], protected_units=["switchboard*"])
        cfg = hosts.load_config()
        self.assertEqual([h["name"] for h in cfg["hosts"]], ["box", "desk"])  # by order
        self.assertEqual(cfg["hosts"][1]["reserve"], {"cores": 1, "ram_gib": 8})
        self.assertTrue(cfg["hosts"][1]["desktop"])

    def test_bad_pattern_is_refused(self):
        write_hosts([], workload_units=["atlas-*; rm -rf /"])
        with self.assertRaises(ValueError):
            hosts.load_config()

    def test_capacity_arithmetic(self):
        h = {"name": "x", "ssh": None, "reserve": {"cores": 2, "ram_gib": 8}}
        doc = {"cpu": {"threads": 32, "cores": 16, "load": [6.0, 5.0, 4.0]},
               "memory": {"total_mib": 128 * 1024, "available_mib": 100 * 1024, "swap_total_mib": 0, "swap_used_mib": 0},
               "gpus": [{"uuid": "u1", "name": "card", "vram_total_mib": 24000, "vram_used_mib": 4000, "display": True,
                         "util": 10, "procs": []}]}
        c = hosts.capacity(h, doc)
        self.assertEqual(c["free_threads"], 32 - 4 - 6)
        self.assertEqual(c["free_ram_gib"], 92.0)
        self.assertEqual(c["gpus"][0]["vram_free_mib"], 20000)
        self.assertTrue(c["gpus"][0]["display"])

    def test_untracked_skips_jobs_protected_and_desktop(self):
        cfg = {"protected_units": ["bobbler-*"], "workload_units": []}
        doc = {"processes": [
            {"pid": 1, "cpu": 90.0, "comm": "python3", "unit": "sb-job-abc.service", "cwd": "/x"},
            {"pid": 2, "cpu": 90.0, "comm": "python3", "unit": "bobbler-relay.service", "cwd": "/x"},
            {"pid": 3, "cpu": 90.0, "comm": "Xorg", "unit": None, "cwd": None},
            {"pid": 4, "cpu": 90.0, "comm": "python3", "unit": "rosa-trace.service", "cwd": "/x"},
            {"pid": 5, "cpu": 90.0, "comm": "python3", "unit": None, "cwd": "/home/me/proj"},
            {"pid": 6, "cpu": 3.0, "comm": "python3", "unit": None, "cwd": "/home/me/proj"}]}
        u = hosts.untracked(cfg, doc)
        self.assertEqual([x["pid"] for x in u], [4, 5])
        self.assertEqual(u[0]["attribution"], "rosa-trace.service")
        self.assertEqual(u[1]["attribution"], "cwd /home/me/proj")


class TickTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        write_hosts([{"name": "here", "ssh": None, "desktop": True, "order": 2},
                     {"name": "far", "ssh": "far-alias", "order": 1},
                     {"name": "gone", "ssh": "unreachable", "order": 3}])

    def test_tick_stores_documents_and_marks_the_down_host(self):
        res = hosts.tick(self.h)
        by = {r["host"]: r for r in res}
        self.assertTrue(by["here"]["ok"] and by["far"]["ok"])
        self.assertFalse(by["gone"]["ok"])
        v = hosts.fleet_view(self.h)
        self.assertEqual([x["name"] for x in v["hosts"]], ["far", "here", "gone"])
        far = v["hosts"][0]
        self.assertTrue(far["reachable"])
        self.assertEqual(far["capacity"]["threads"], os.cpu_count())
        self.assertIn("No route to host", v["hosts"][2]["error"])
        self.assertFalse(v["hosts"][2]["reachable"])
        s = hosts.summary(self.h)
        self.assertEqual(len(s["hosts"]), 3)
        self.assertIsNone(s["halt"])
        self.assertEqual(self.h.s.one("SELECT COUNT(*) AS n FROM host_samples WHERE host='here'")["n"], 1)

    def test_second_tick_adds_cpu_rates_and_keeps_last_doc_of_a_host_that_drops(self):
        hosts.tick(self.h)
        write_hosts([{"name": "here", "ssh": None}, {"name": "far", "ssh": "unreachable"}])
        hosts.tick(self.h)
        v = {x["name"]: x for x in hosts.fleet_view(self.h)["hosts"]}
        self.assertFalse(v["far"]["reachable"])
        self.assertIsNotNone(v["far"].get("capacity"), "last good document is kept")
        self.assertNotIn("gone", v, "a host removed from the file leaves the table")
        doc = json.loads(self.h.s.one("SELECT doc FROM hosts WHERE name='here'")["doc"])
        self.assertIn("rates", doc)


class HaltTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        shutil.rmtree(os.environ["SB_STATE"], ignore_errors=True)
        write_hosts([{"name": "here", "ssh": None}, {"name": "far", "ssh": "far-alias"},
                     {"name": "gone", "ssh": "unreachable"}],
                    workload_units=["sbtest-work-*"], protected_units=["sbtest-keep-*"])

    def flag(self):
        return os.path.join(os.environ["SB_STATE"], "HALT")

    def test_halt_flags_every_reachable_host_and_resume_clears(self):
        res = hosts.halt(self.h, "tester", "lag", None, False)
        by = {r["host"]: r for r in res["results"]}
        self.assertTrue(by["here"]["flag"] and by["far"]["flag"])
        self.assertFalse(by["gone"]["ok"])
        self.assertTrue(os.path.exists(self.flag()))
        self.assertIn("HALT by tester", open(self.flag()).read())
        st = hosts.halt_state(self.h)
        self.assertEqual(sorted(st["hosts"]), ["far", "here"])
        self.assertEqual(st["pending"], ["gone"])
        res = hosts.resume(self.h, "tester")
        self.assertFalse(os.path.exists(self.flag()))
        self.assertIsNone(hosts.halt_state(self.h))
        self.assertIsNone(res["halt"])

    def test_partial_resume_keeps_the_rest_halted(self):
        hosts.halt(self.h, "tester", "lag", ["here", "far"], False)
        res = hosts.resume(self.h, "tester", ["far"])
        self.assertEqual(res["halt"]["hosts"], ["here"])

    def test_unknown_host_is_refused(self):
        with self.assertRaises(LookupError):
            hosts.halt(self.h, "tester", "", ["nowhere"], False)

    def test_workloads_needs_patterns(self):
        write_hosts([{"name": "here", "ssh": None}])
        with self.assertRaises(ValueError):
            hosts.halt(self.h, "tester", "", None, True)

    @unittest.skipUnless(have_user_systemd(), "needs a systemd user manager")
    def test_halt_stops_tracked_jobs_and_disables_matching_workloads_only(self):
        tag = uuid.uuid4().hex[:8]
        units = {"job": f"sb-job-test{tag}", "work": f"sbtest-work-{tag}", "keep": f"sbtest-keep-{tag}"}
        for u in units.values():
            subprocess.run(["systemd-run", "--user", "--quiet", "--unit", u, "sleep", "300"], check=True)
        try:
            res = hosts.halt(self.h, "tester", "test", ["here"], True)
            r = res["results"][0]
            self.assertIn(units["job"] + ".service", r["stopped"])
            self.assertIn(units["work"] + ".service", r["disabled"])
            self.assertNotIn(units["keep"] + ".service", r["disabled"])
            active = subprocess.run(["systemctl", "--user", "is-active", units["keep"]], capture_output=True,
                                    text=True).stdout.strip()
            self.assertEqual(active, "active", "protected unit untouched")
        finally:
            for u in units.values():
                subprocess.run(["systemctl", "--user", "stop", u], capture_output=True)
            hosts.resume(self.h, "tester")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.h = new_hub()
        self.d = os.environ["SB_SYSTEMD_USER_DIR"]
        shutil.rmtree(self.d, ignore_errors=True)
        os.makedirs(self.d)
        for name in ("atlas-sweep.service", "atlas-sweep.timer", "atlas-bobbler.service", "devbox-compositor.service"):
            with open(os.path.join(self.d, name), "w") as f:
                f.write("[Unit]\nDescription=test\n")
        write_hosts([{"name": "here", "ssh": None}, {"name": "far", "ssh": "far-alias"}],
                    workload_units=["atlas-*"], protected_units=["atlas-bobbler*", "devbox-*"])

    def test_install_list_remove(self):
        res = hosts.guards(self.h, "install")
        for r in res["results"]:
            self.assertEqual(r["units"], ["atlas-sweep.service", "atlas-sweep.timer"], r)
        conf = os.path.join(self.d, "atlas-sweep.service.d", "switchboard-halt.conf")
        self.assertEqual(open(conf).read(), hosts.GUARD)
        self.assertFalse(os.path.exists(os.path.join(self.d, "atlas-bobbler.service.d")))
        self.assertEqual(hosts.guards(self.h, "list", ["here"])["results"][0]["units"],
                         ["atlas-sweep.service", "atlas-sweep.timer"])
        res = hosts.guards(self.h, "remove", ["here"])
        self.assertEqual(res["results"][0]["units"], ["atlas-sweep.service", "atlas-sweep.timer"])
        self.assertFalse(os.path.exists(conf))

    def test_install_needs_patterns(self):
        write_hosts([{"name": "here", "ssh": None}])
        with self.assertRaises(ValueError):
            hosts.guards(self.h, "install")


class HubRouteTests(unittest.TestCase):
    def test_overview_carries_the_fleet_summary(self):
        h = new_hub()
        write_hosts([{"name": "here", "ssh": None, "desktop": True}])
        hosts.tick(h)
        ov = h.overview()
        self.assertEqual(ov["fleet"]["hosts"][0]["name"], "here")
        self.assertTrue(ov["fleet"]["hosts"][0]["reachable"])
        self.assertIsNone(ov["fleet"]["halt"])

    def test_add_host_probes_before_writing(self):
        write_hosts([])
        with self.assertRaises(ValueError):
            hosts.add_host({"name": "nope", "ssh": "unreachable"})
        res = hosts.add_host({"name": "twin", "ssh": "twin-alias", "order": 1})
        self.assertTrue(res["host"]["reachable"])
        self.assertEqual(hosts.load_config()["hosts"][0]["name"], "twin")
        self.assertIn("wsl.exe -d Ubuntu", hosts.wsl_keepalive("Ubuntu"))


if __name__ == "__main__":
    unittest.main()
