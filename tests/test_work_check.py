"""A work item can carry a check: a command the hub runs when the item is marked done. It closes only if that passes,
so 'done' is something the hub saw. And `sb whoami`: an agent can read its own launch note again."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="sb-check-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_SYSTEMD_USER_DIR"] = os.path.join(TMP, "units")
os.environ["SB_HOSTS"] = os.path.join(TMP, "hosts.json")
os.environ.setdefault("SB_WORKSPACE", os.path.join(TMP, "ws"))
from switchboard import barrier, fleet, shared  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.h = Hub(Store(os.path.join(self.dir, "hub.db")))
        self.h.s.upsert_project({"id": "alpha", "name": "Alpha", "match": {"cwd": [self.dir]}})
        for aid in ("lead", "tasker"):
            self.h.s.add_agent({"id": aid, "project": "alpha", "title": aid, "provider": "antigravity", "account": "g",
                                "mode": "terminal", "cwd": self.dir, "state": "idle"})

    def test_an_item_with_a_check_closes_only_when_the_hub_sees_it_pass(self):
        w = fleet.work_add(self.h, {"title": "write out.txt", "from": "lead", "agent": "tasker",
                                    "check": "grep -q ready out.txt"})
        self.assertEqual((w["check_cmd"], w["check_by"]), ("grep -q ready out.txt", "lead"))
        with self.assertRaisesRegex(PermissionError, "stays todo: its check failed"):  # said done; it is not
            fleet.work_update(self.h, w["id"], {"status": "done", "from": "tasker"})
        again = fleet.work_item(self.h, w["id"])
        self.assertEqual(again["status"], "todo")
        self.assertIn("check FAILED (tasker asked for done)", again["detail"])
        fleet.work_update(self.h, w["id"], {"status": "doing", "from": "tasker"})  # other changes need no check
        with open(os.path.join(self.dir, "out.txt"), "w") as f:  # the check runs in the project's folder
            f.write("ready\n")
        done = fleet.work_update(self.h, w["id"], {"status": "done", "from": "tasker", "note": "wrote it"})
        self.assertEqual(done["status"], "done")
        self.assertIn("check passed", done["detail"])
        self.assertIn("wrote it", done["detail"])

    def test_only_whoever_set_a_check_or_the_owner_changes_it(self):
        w = fleet.work_add(self.h, {"title": "t", "from": "lead", "agent": "tasker", "check": "false"})
        for change in ("", "true"):
            with self.assertRaises(PermissionError):
                fleet.work_update(self.h, w["id"], {"check": change, "from": "tasker"})
        self.assertEqual(fleet.work_update(self.h, w["id"], {"check": "true", "from": "lead"})["check_cmd"], "true")
        self.assertIsNone(fleet.work_update(self.h, w["id"], {"check": "", "from": "owner"})["check_cmd"])
        mine = fleet.work_update(self.h, w["id"], {"check": "true", "from": "tasker"})  # none set: anyone may set one
        self.assertEqual(mine["check_by"], "tasker")
        self.assertEqual(fleet.work_update(self.h, w["id"], {"status": "done", "from": "tasker"})["status"], "done")

    def test_an_item_without_a_check_closes_as_before_and_a_slow_check_fails(self):
        w = fleet.work_add(self.h, {"title": "plain", "from": "tasker"})
        self.assertEqual(fleet.work_update(self.h, w["id"], {"status": "done", "from": "tasker"})["status"], "done")
        slow = fleet.work_add(self.h, {"title": "slow", "from": "lead", "check": "sleep 5"})
        old, fleet.CHECK_TIMEOUT_S = fleet.CHECK_TIMEOUT_S, 0.3
        try:
            with self.assertRaisesRegex(PermissionError, "no result in"):
                fleet.work_update(self.h, slow["id"], {"status": "done", "from": "lead"})
        finally:
            fleet.CHECK_TIMEOUT_S = old

    def test_whoami_is_open_to_agents_and_is_the_launch_note(self):
        self.assertTrue(barrier.agent_may("GET", "/api/whoami", "alpha"))
        self.assertIn("You are agent 'tasker' on project 'alpha'", shared.launch_note(self.h.s.agent("tasker")))


if __name__ == "__main__":
    unittest.main()
