"""Unit tests for POST /api/open, local/partial path resolution, Thunar folder launch, and barrier isolation.
Run: python3 -m unittest tests.test_open_link
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
_TEST_STATE = tempfile.mkdtemp(prefix="sb-open-state-")
os.environ["SB_STATE"] = _TEST_STATE

from switchboard import barrier, hub  # noqa: E402
from switchboard.store import Store  # noqa: E402


class OpenLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sb-open-")
        self.ws = os.path.join(self.tmp, "ws")
        self.proj = os.path.join(self.ws, "experiments", "demo")
        self.proj2 = os.path.join(self.ws, "experiments", "demo-extra")
        self.sub = os.path.join(self.proj, "src", "nested")
        os.makedirs(self.sub)
        os.makedirs(self.proj2)
        self.f_nested = os.path.join(self.sub, "widget.py")
        with open(self.f_nested, "w") as f:
            f.write("print('ok')\n")
        self.f_space = os.path.join(self.proj, "My Notes.md")
        with open(self.f_space, "w") as f:
            f.write("# notes\n")
        self.f_extra = os.path.join(self.proj2, "extra.py")
        with open(self.f_extra, "w") as f:
            f.write("x = 1\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_file_uri_and_line_suffixes_and_partial_paths(self):
        # file:// URI with #L10-L20 and %20
        uri = f"file://{self.proj}/My%20Notes.md#L1-L5"
        self.assertEqual(hub.resolve_local_path(uri, agent_cwd=self.proj, workspace=self.ws), self.f_space)

        # file:// URI with :line:col suffix and localhost authority
        self.assertEqual(hub.resolve_local_path(f"file://{self.f_nested}:42:7", agent_cwd=self.proj, workspace=self.ws),
                         self.f_nested)
        self.assertEqual(hub.resolve_local_path(f"file://localhost{self.f_nested}", agent_cwd=self.proj, workspace=self.ws),
                         self.f_nested)

        # Absolute path with :line:col
        self.assertEqual(hub.resolve_local_path(f"{self.f_nested}:42:7", agent_cwd=self.proj, workspace=self.ws),
                         self.f_nested)

        # Relative path from agent_cwd
        self.assertEqual(hub.resolve_local_path("src/nested/widget.py:10", agent_cwd=self.proj, workspace=self.ws),
                         self.f_nested)

        # Partial path rooted at a parent dir above agent_cwd (experiments/demo/src/nested/widget.py)
        self.assertEqual(hub.resolve_local_path("demo/src/nested/widget.py", agent_cwd=self.sub, workspace=self.ws),
                         self.f_nested)

        # Bare filename / deep partial suffix resolved via bounded shallow search inside agent_cwd
        self.assertEqual(hub.resolve_local_path("widget.py#L9", agent_cwd=self.proj, workspace=self.ws),
                         self.f_nested)
        self.assertEqual(hub.resolve_local_path("nested/widget.py", agent_cwd=self.proj, workspace=self.ws),
                         self.f_nested)

        # Multiple project_cwd roots (as stored in project.match.cwd list)
        self.assertEqual(hub.resolve_local_path("extra.py", agent_cwd=self.proj,
                                                project_cwd=[self.proj, self.proj2], workspace=self.ws),
                         self.f_extra)

        # Folder path resolves to exact directory
        self.assertEqual(hub.resolve_local_path("src/nested/", agent_cwd=self.proj, workspace=self.ws), self.sub)

        # Missing path returns None
        self.assertIsNone(hub.resolve_local_path("does/not/exist.py", agent_cwd=self.proj, workspace=self.ws))

    def test_open_desktop_dispatches_url_dir_and_file_and_blocks_bad_schemes(self):
        calls = []

        def fake_spawn(argv, **kw):
            calls.append((argv, kw.get("env") or {}))

        expected_cmd, expected_opener = hub._desktop_open_cmd()

        # Web URL -> gio open (or xdg-open fallback) with DISPLAY in env
        r_url = hub.open_desktop("https://example.com/path?a=1", spawn=fake_spawn)
        self.assertEqual(r_url["kind"], "url")
        self.assertEqual(r_url["opener"], expected_opener)
        self.assertEqual(calls[-1][0], [*expected_cmd, "https://example.com/path?a=1"])
        self.assertTrue(calls[-1][1].get("DISPLAY"))

        # Directory -> thunar / xdg-open
        r_dir = hub.open_desktop("src/nested", agent_cwd=self.proj, workspace=self.ws, spawn=fake_spawn)
        self.assertEqual(r_dir["kind"], "dir")
        self.assertEqual(r_dir["path"], self.sub)
        self.assertEqual(calls[-1][0][1], self.sub)

        # File -> gio open (or xdg-open fallback) on resolved path
        r_file = hub.open_desktop("widget.py:12", agent_cwd=self.proj, workspace=self.ws, spawn=fake_spawn)
        self.assertEqual(r_file["kind"], "file")
        self.assertEqual(r_file["path"], self.f_nested)
        self.assertEqual(r_file["opener"], expected_opener)
        self.assertEqual(calls[-1][0], [*expected_cmd, self.f_nested])

        # Unsupported schemes rejected
        for bad in ("javascript:alert(1)", "data:text/html,hi", "mailto:a@b.com"):
            with self.assertRaises(ValueError):
                hub.open_desktop(bad, spawn=fake_spawn)

        # Missing local path raises LookupError -> HTTP 404
        with self.assertRaises(LookupError):
            hub.open_desktop("nope/missing.py", agent_cwd=self.proj, workspace=self.ws, spawn=fake_spawn)

    def test_open_route_with_project_dict_match_and_cwd_list(self):
        store = Store(os.path.join(self.tmp, "hub.db"))
        store.upsert_project({"id": "demo", "match": {"cwd": [self.proj, self.proj2]}})
        store.add_agent({"id": "agent-demo", "project": "demo", "title": "demo", "provider": "antigravity",
                         "mode": "terminal", "cwd": self.proj, "state": "idle"})
        h = hub.Hub(store=store)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.make_handler(h))
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        spawned = []
        real_open = hub.open_desktop

        def fake_open(target, agent_cwd=None, project_cwd=None, workspace=None, spawn=None):
            return real_open(target, agent_cwd=agent_cwd, project_cwd=project_cwd, workspace=self.ws,
                             spawn=lambda argv, **kw: spawned.append(argv))

        try:
            with mock.patch.object(hub, "ALLOWED_HOSTS", {f"127.0.0.1:{port}"}), \
                 mock.patch.object(barrier, "identify",
                                   return_value=barrier.Caller(agent=None, pid=None, unmanaged=False, owner_surface=True)), \
                 mock.patch.object(hub, "open_desktop", side_effect=fake_open):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                body = json.dumps({"target": "extra.py", "agent": "agent-demo"})
                conn.request("POST", "/api/open", body=body,
                             headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
                resp = conn.getresponse()
                data = json.loads(resp.read().decode())
                self.assertEqual(resp.status, 200, data)
                self.assertEqual(data["kind"], "file")
                self.assertEqual(data["path"], self.f_extra)
                self.assertEqual(spawned[-1][-1], self.f_extra)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_open_route_is_owner_only(self):
        self.assertFalse(barrier.agent_may("POST", "/api/open", "docs"))
        self.assertFalse(barrier.agent_may("POST", "/api/open", "ops"))


if __name__ == "__main__":
    unittest.main()
