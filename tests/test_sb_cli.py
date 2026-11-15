"""The sb CLI: `sb run ... -- cmd` drops only the leading separator, so a command's own `--` arguments survive.
Run: python3 -m unittest tests.test_sb_cli
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SB = os.path.join(os.path.dirname(HERE), "bin", "sb")


class Capture(http.server.BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Capture.bodies.append(json.loads(self.rfile.read(n) or b"{}"))
        out = json.dumps({"id": "job-x", "state": "queued", "name": "x", "threads": 1, "mem_mib": 1024,
                          "reason": "test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class RunArgvTests(unittest.TestCase):
    def setUp(self):
        Capture.bodies = []
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Capture)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env = {**os.environ, "SB_HUB": f"http://127.0.0.1:{self.srv.server_port}"}
        self.env.pop("SB_AGENT", None)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def argv_sent(self, *cmd):
        subprocess.run([sys.executable, SB, "run", "--name", "x", *cmd], env=self.env, capture_output=True,
                       text=True, timeout=30)
        posts = [b for b in Capture.bodies if "argv" in b]
        self.assertTrue(posts, "sb run posted a job")
        return posts[-1]["argv"]

    def test_inner_double_dash_survives(self):
        self.assertEqual(self.argv_sent("--", "python3", "prog.py", "--flag", "a", "--", "inner"),
                         ["python3", "prog.py", "--flag", "a", "--", "inner"])

    def test_without_separator(self):
        self.assertEqual(self.argv_sent("python3", "prog.py", "--", "inner"), ["python3", "prog.py", "--", "inner"])


if __name__ == "__main__":
    unittest.main()
