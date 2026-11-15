"""An idle GPU lease loses its standing: it halves per half-life of idleness, its agent is asked at each halving, and
after three the card is given back. Use restores it; a card nobody can see does not count against the lease."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="sb-leasedecay-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_SYSTEMD_USER_DIR"] = os.path.join(TMP, "units")
os.environ["SB_HOSTS"] = os.path.join(TMP, "hosts.json")
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))

from switchboard import leasedecay  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store, iso_from_epoch  # noqa: E402

CFG = {"hosts": [{"name": "homebox", "ssh": None}], "jobs": {"lease_half_life_min": 15}}
LEASE = {"id": "gpu-1", "label": "train", "project": "alpha", "status": "running", "uuid": "GPU-a", "host": "gpu"}


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class LeaseDecayTests(unittest.TestCase):
    def setUp(self):
        self.h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        self.h.s.upsert_project({"id": "alpha", "name": "Alpha"})
        self.h.s.add_agent({"id": "a1", "project": "alpha", "title": "a1", "provider": "claude", "account": "one",
                            "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        self.t = time.time()

    def probe(self, util, at=None, ok=1):
        doc = {"gpus": [{"uuid": "GPU-a", "index": 4, "util": util}]}
        self.h.s.x("INSERT OR REPLACE INTO hosts (name, doc, probed_at, ok) VALUES ('gpubox', ?, ?, ?)",
                   (json.dumps(doc), iso_from_epoch(at or self.t), ok))

    def run_min(self, minutes, util=0, lease=LEASE, fresh=True):
        did = []
        for _ in range(minutes):
            self.t += 60
            if fresh:
                self.probe(util)
            did += leasedecay.tick(self.h, now=self.t, status={"jobs": [lease]}, cfg=CFG)
        return did

    def test_standing_halves_per_half_life(self):
        self.assertEqual(leasedecay.standing(0, 900), 1.0)
        self.assertEqual(leasedecay.standing(900, 900), 0.5)
        self.assertEqual(leasedecay.standing(2700, 900), 0.125)

    def test_idle_lease_is_asked_at_each_halving_then_released(self):
        with mock.patch.object(leasedecay.placers, "wb_finish", return_value=(True, "")) as fin:
            self.run_min(1)  # first sighting: the clock starts here
            self.assertEqual(self.run_min(14), [])
            self.assertEqual(self.run_min(1), [("gpu-1", "asked")])
            msgs = self.h.s.messages("a1")
            self.assertEqual(len(msgs), 1)  # no hub job: whoever answers for the project is asked
            self.assertIn("conscientious", msgs[0]["body"])
            self.assertIn("50.0%", msgs[0]["body"])
            self.assertEqual(self.run_min(15), [("gpu-1", "asked")])
            self.assertEqual(leasedecay.view(self.h, CFG)["gpu-1"], {"idle_min": 30, "standing": 0.25})
            fin.assert_not_called()
            self.assertEqual(self.run_min(15), [("gpu-1", "released")])
            self.assertEqual(fin.call_args[0][2:4], ("gpu-1", "released"))
        self.assertEqual(len(self.h.s.messages("a1")), 3)
        self.assertEqual(self.h.s.setting(leasedecay.SETTING), {})

    def test_use_restores_standing_in_full(self):
        self.run_min(20)
        self.run_min(1, util=80)
        self.assertEqual(leasedecay.view(self.h, CFG), {})
        self.assertEqual(self.run_min(14), [])  # the clock started over
        self.assertEqual(self.run_min(1), [("gpu-1", "asked")])

    def test_reported_work_counts_as_use_when_probes_read_zero(self):
        # sub-second bursts: every spot probe reads 0%, but the heartbeat keeps reporting new requests
        self.run_min(1)
        for _ in range(60):
            lease = dict(LEASE, work_changed=self.t)
            self.assertEqual(self.run_min(1, lease=lease), [])
        self.assertEqual(leasedecay.view(self.h, CFG), {})
        stale = dict(LEASE, work_changed=self.t - 900)  # nothing reported for a half-life: idle again
        self.assertEqual(self.run_min(14, lease=stale), [])
        self.assertEqual(self.run_min(1, lease=stale), [("gpu-1", "asked")])

    def test_unseen_card_does_not_count_against_the_lease(self):
        self.probe(0, at=self.t - 3600)  # the last probe is an hour old
        self.assertEqual(self.run_min(60, fresh=False), [])
        self.assertEqual(leasedecay.view(self.h, CFG), {})

    def test_hub_job_is_stopped_and_its_own_agent_told(self):
        self.h.s.add_agent({"id": "a2", "project": "alpha", "title": "a2", "provider": "claude", "account": "one",
                            "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        self.h.s.x("INSERT INTO jobs (id, name, project, agent, state, board_id, created_at) VALUES "
                   "('job-1', 'train', 'alpha', 'a2', 'running', 'gpu-1', ?)", (iso_from_epoch(self.t),))
        with mock.patch.object(leasedecay.jobs, "stop") as stop:
            self.run_min(46)
        stop.assert_called_once()
        self.assertEqual(stop.call_args[0][1:3], ("job-1", "switchboard"))
        self.assertEqual(len(self.h.s.messages("a2")), 3)
        self.assertIn("sb stop job-1", self.h.s.messages("a2")[-1]["body"] + self.h.s.messages("a2")[0]["body"])
        self.assertEqual(self.h.s.messages("a1"), [])

    def test_off_switch(self):
        self.probe(0)
        self.assertEqual(leasedecay.tick(self.h, now=self.t + 9999, status={"jobs": [LEASE]},
                                         cfg={"hosts": [], "jobs": {"lease_decay": False}}), [])


if __name__ == "__main__":
    unittest.main()
