"""The hub notices a host the GPU workboard has gone blind to, repairs its telemetry and pages the owner."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sbbw-")
os.environ["SB_STATE"] = TMP
sys.path.insert(0, ROOT)

from switchboard import boardwatch, placers  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402

CFG = {"hosts": [{"name": "gpubox", "ssh": "gpu"}, {"name": "homebox", "ssh": None}]}
ROWS = {"gpubox": {"ok": 1, "doc": {"gpus": [{"uuid": "U1"}, {"uuid": "U2"}]}},
        "homebox": {"ok": 1, "doc": {"gpus": [{"uuid": "U3"}]}}}


def board(fresh):
    return {"available": True, "cards": [{"uuid": "U1", "label": "gpu-0", "fresh": fresh},
                                         {"uuid": "U2", "label": "gpu-1", "fresh": fresh},
                                         {"uuid": "U3", "label": "homebox-3090", "fresh": True}]}


class BoardWatch(unittest.TestCase):
    def test_stale_host_is_repaired_then_paged_then_cleared(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        ran = []

        def run(host, script, timeout=0):
            ran.append((host["name"], script))
            return 0, "timer=enabled/active publish=success\n", ""

        self.assertEqual(boardwatch.check(h, CFG, board(True), ROWS, now=1000, run=run), [])
        self.assertEqual(boardwatch.check(h, CFG, board(False), ROWS, now=1000, run=run), [("repair", "gpubox")])
        self.assertIn("enable --now gpu-workboard-telemetry.timer", ran[0][1])
        self.assertEqual(boardwatch.check(h, CFG, board(False), ROWS, now=1030, run=run), [])  # no repair storm
        self.assertEqual(h.notifications()["need"], 0)
        did = boardwatch.check(h, CFG, board(False), ROWS, now=1000 + boardwatch.PAGE_S, run=run)
        self.assertEqual(did, [("repair", "gpubox"), ("paged", "gpubox")])
        boardwatch.check(h, CFG, board(False), ROWS, now=1000 + boardwatch.PAGE_S + 300, run=run)
        need = [n for n in h.notifications()["items"] if n["level"] == "need"]
        self.assertEqual(len(need), 1)  # paged once, not every tick
        self.assertIn("cannot see gpubox: 2 card(s)", need[0]["text"])
        self.assertEqual(boardwatch.check(h, CFG, board(True), ROWS, now=2000, run=run), [("recovered", "gpubox")])

    def test_no_verdict_without_the_board_or_the_host(self):
        h = Hub(Store(os.path.join(tempfile.mkdtemp(dir=TMP), "hub.db")))
        run = lambda *a, **k: self.fail("nothing to repair")  # noqa: E731
        self.assertEqual(boardwatch.check(h, CFG, {"available": True, "error": "x"}, ROWS, now=1, run=run), [])
        down = {**ROWS, "gpubox": {"ok": 0, "doc": ROWS["gpubox"]["doc"]}}
        self.assertEqual(boardwatch.check(h, CFG, board(False), down, now=1, run=run), [])

    def test_preempted_rows_are_history_not_open_leases(self):
        data = {"fleet": {"cards": []}, "jobs": [{"id": "a", "status": "running"}, {"id": "b", "status": "preempted"},
                                                 {"id": "c", "status": "queued"}, {"id": "d", "status": "released"}]}
        cfg = {"hosts": [{"name": "homebox", "ssh": None}]}
        old = placers._wb, placers.available
        placers._wb, placers.available = (lambda *a, **k: (0, data, "")), (lambda c: {"gpu_workboard": True, "cpu_run": False})
        try:
            wb = placers.wb_status(cfg)
        finally:
            placers._wb, placers.available = old
        self.assertEqual(([j["id"] for j in wb["jobs"]], [j["id"] for j in wb["preempted"]]), (["a", "c"], ["b"]))


if __name__ == "__main__":
    unittest.main()
