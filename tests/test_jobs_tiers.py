"""Tiered priority (hosts.json jobs.priority_projects, then everything else) and claw-back of unused holds."""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sb-tiers-")
os.environ.setdefault("SB_STATE", os.path.join(TMP, "state"))
os.environ.setdefault("SB_HOSTS", os.path.join(TMP, "hosts.json"))
assert "/.config/" not in os.environ["SB_HOSTS"]

from switchboard import jobs  # noqa: E402

CFG = {"jobs": {"priority_projects": ["ops", "lab", "docs"], "slice_cpu_weight": 20,
                "display_gpu": "avoid"}}


def iso(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


class Tiers(unittest.TestCase):
    def test_owner_order(self):
        self.assertEqual([jobs.tier(CFG, p) for p in ("ops", "lab", "docs", "cad", None)],
                         [0, 1, 2, jobs.OTHER_TIER, jobs.OTHER_TIER])
        # an unlisted project never lands on a listed tier, whatever the list length
        self.assertEqual(jobs.tier({"jobs": {"priority_projects": ["ops"]}}, "alpha"), jobs.OTHER_TIER)


class ClawBack(unittest.TestCase):
    def test_threads_follow_use_after_settling_never_above_declared(self):
        self.assertEqual(jobs.thread_hold(32, 100.0, iso(60)), 32)                  # still settling
        ran = jobs.CPU_SETTLE_S + 600
        self.assertEqual(jobs.thread_hold(32, 4.0 * ran, iso(ran)), 5)              # 4 cores used -> 5 booked
        self.assertEqual(jobs.thread_hold(32, 0.0, iso(ran)), 1)                    # idle: at least 1
        self.assertEqual(jobs.thread_hold(8, 40.0 * ran, iso(ran)), 8)              # never above the declaration
        self.assertEqual(jobs.thread_hold(8, None, iso(ran)), 8)                    # no reading: the declaration

    def test_vram_follows_use_once_settled(self):
        self.assertEqual(jobs.gpu_hold_mib(30000, 4000, settled=False), 30000)
        self.assertEqual(jobs.gpu_hold_mib(30000, 4000, settled=True), 6048)        # +2 GiB beats x1.25 here
        self.assertEqual(jobs.gpu_hold_mib(30000, 20000, settled=True), 25000)
        self.assertEqual(jobs.gpu_hold_mib(30000, 29000, settled=True), 30000)


class Launch(unittest.TestCase):
    def script(self, project):
        job = {"id": "job-1", "name": "n", "project": project, "agent": "a", "unit": "sb-job-job-1.service",
               "cwd": "/tmp", "argv": json.dumps(["true"]), "mem_mib": 200 * 1024, "hours": 12, "cpus": None}
        h = {"name": "gpubox", "desktop": True, "reserve": {"ram_gib": 8}}
        return jobs._launch_script(CFG, h, {"memory": {"total_mib": 400000}}, job, None)

    def test_the_first_tier_runs_in_its_own_slice_at_normal_nice_and_asks_for_ram(self):
        s = self.script("ops")
        self.assertIn("switchboard-priority.slice", s)
        self.assertIn("CPUWeight=1000", s)
        self.assertIn("Nice=0", s)
        self.assertIn("fleet-priority/demand", s)
        self.assertIn('"ram_gib": 200.0', s)

    def test_lower_tiers_keep_the_jobs_slice(self):
        chem, other = self.script("lab"), self.script("cad")
        self.assertIn("Nice=4", chem)
        self.assertIn("CPUWeight=400", chem)
        self.assertNotIn("fleet-priority/demand", chem)
        self.assertIn("Nice=10", other)
        self.assertIn("CPUWeight=20", other)
        for s in (chem, other):
            self.assertIn("switchboard-jobs.slice", s)
            self.assertNotIn("switchboard-priority.slice", s)


if __name__ == "__main__":
    unittest.main()
