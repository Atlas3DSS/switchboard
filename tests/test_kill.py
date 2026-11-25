"""Killing a project removes it entirely: agents, their conversations, the hub's record; the seed does not bring it back."""
import json
import os
import shutil
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix="sb-kill-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_WORKSPACE"] = os.path.join(TMP, "ws")

from switchboard import discovery, kill  # noqa: E402
from switchboard import hub as hubmod  # noqa: E402
from switchboard.hub import Hub  # noqa: E402
from switchboard.store import Store  # noqa: E402


class KillTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=TMP)
        self.h = Hub(Store(os.path.join(self.d, "hub.db")))
        s = self.h.s
        for pid in ("alpha", "beta"):
            s.upsert_project({"id": pid, "name": pid.title(), "match": {"cwd": [os.path.join(self.d, pid)]}})
            s.add_agent({"id": pid + "-1", "project": pid, "title": pid, "provider": "claude", "account": "one",
                         "mode": "terminal", "session_id": "s-" + pid, "cwd": os.path.join(self.d, pid), "state": "idle"})
            s.x("INSERT INTO notes(project, agent, text, created_at) VALUES(?,?,?,?)", (pid, pid + "-1", "n", "t"))
            s.event(pid + "-1", "system", "hello")
        self.tx = os.path.join(self.d, "claude", "projects", "-x", "s-alpha.jsonl")
        os.makedirs(os.path.dirname(self.tx))
        open(self.tx, "w").write("{}\n")
        self._tp = discovery.transcript_path
        discovery.transcript_path = lambda provider, sid, account=None: self.tx if sid == "s-alpha" else None
        self.mem = {pid: os.path.join(self.d, "mem", pid, "memory") for pid in ("alpha", "beta")}
        for d in self.mem.values():
            os.makedirs(d)
            open(os.path.join(d, "MEMORY.md"), "w").close()
        self._md = kill.memory_dir
        kill._MEMDIRS.clear()
        kill.memory_dir = lambda a: self.mem[a["project"]]
        kill._claude_config_dirs = lambda: []

    def tearDown(self):
        discovery.transcript_path = self._tp
        kill.memory_dir = self._md

    def test_kill_takes_the_project_its_agents_and_sessions_and_leaves_the_others(self):
        r = kill.kill_project(self.h, "alpha")
        s = self.h.s
        self.assertEqual((r["agents"], r["sessions"]), (1, 1))
        self.assertIsNone(s.project("alpha"))
        self.assertFalse([a for a in s.agents(include_gone=True) if a["project"] == "alpha"])
        self.assertFalse(s.q("SELECT 1 FROM notes WHERE project='alpha'"))
        self.assertFalse(s.q("SELECT 1 FROM events WHERE agent='alpha-1'"))
        self.assertFalse(os.path.exists(self.tx))
        self.assertNotIn("kept_at", r)
        self.assertFalse(any("s-alpha" in n for _, _, fs in os.walk(TMP) for n in fs))  # no copy anywhere
        self.assertTrue(s.project("beta") and s.agent("beta-1"))
        self.assertTrue(s.q("SELECT 1 FROM notes WHERE project='beta'"))
        self.assertTrue(s.q("SELECT 1 FROM events WHERE agent='beta-1'"))

    def test_memory_folders_go_with_the_project_unless_another_projects_agent_writes_there(self):
        r = kill.kill_project(self.h, "alpha")
        self.assertFalse(os.path.exists(self.mem["alpha"]))
        self.assertTrue(os.path.exists(self.mem["beta"]))
        self.assertEqual(r["memory_shared"], [])
        self.h.s.upsert_project({"id": "gamma", "name": "Gamma"})
        self.h.s.add_agent({"id": "gamma-1", "project": "gamma", "title": "g", "provider": "claude", "account": "one",
                            "mode": "terminal", "session_id": "s-gamma", "cwd": os.path.join(self.d, "gamma"),
                            "state": "idle"})
        kill._MEMDIRS.clear()
        kill.memory_dir = lambda a: self.mem["beta"]  # gamma and beta share one folder
        r = kill.kill_project(self.h, "gamma")
        self.assertEqual(r["memory_shared"], [self.mem["beta"]])
        self.assertTrue(os.path.exists(self.mem["beta"]))

    def test_the_seed_file_does_not_bring_a_killed_project_back_until_it_is_created_again(self):
        seed = os.path.join(self.d, "projects.json")
        json.dump([{"id": "alpha", "name": "Alpha"}], open(seed, "w"))
        old, hubmod.SEED = hubmod.SEED, seed
        try:
            kill.kill_project(self.h, "alpha")
            self.h.seed()
            self.assertIsNone(self.h.s.project("alpha"))
            kill.revive(self.h, "alpha")
            self.h.seed()
            self.assertTrue(self.h.s.project("alpha"))
        finally:
            hubmod.SEED = old

    def test_switchboard_and_unknown_projects_are_refused(self):
        self.h.s.upsert_project({"id": "switchboard", "name": "Switchboard"})
        with self.assertRaises(PermissionError):
            kill.kill_project(self.h, "switchboard")
        with self.assertRaises(LookupError):
            kill.kill_project(self.h, "nope")


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
