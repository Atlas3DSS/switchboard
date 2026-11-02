"""The hub's local boundary: only requests addressed to this machine's loopback name (no DNS rebinding), never from
another site's page (Origin), JSON for writes; the CLI, the page itself and the phone gate keep working. And the
WebSocket reader refuses frames a browser would never send. Run: python3 -m unittest tests.test_boundary
"""
import http.client
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbboundary-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")

from switchboard import hub as hubmod, ws  # noqa: E402
from switchboard.hub import Hub, make_handler  # noqa: E402
from switchboard.store import Store  # noqa: E402


class HubBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Hub(Store(os.path.join(TMP, "hub.db")))
        cls.h.s.add_agent({"id": "a1", "project": "p", "title": "a", "provider": "claude", "account": "x",
                           "mode": "terminal", "session_id": None, "cwd": TMP, "state": "idle"})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.h))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_port
        cls.old = hubmod.ALLOWED_HOSTS
        hubmod.ALLOWED_HOSTS = {f"127.0.0.1:{cls.port}", f"localhost:{cls.port}"}

    @classmethod
    def tearDownClass(cls):
        hubmod.ALLOWED_HOSTS = cls.old
        cls.srv.shutdown()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        data = body.encode() if isinstance(body, str) else body
        if data is not None:
            h["Content-Length"] = str(len(data))
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_cli_and_own_page_work(self):
        self.assertEqual(self.req("GET", "/api/agents"), 200)  # sb: no Origin
        self.assertEqual(self.req("POST", "/api/notes", json.dumps({"text": "hi", "from": "owner", "project": "p"}),
                                  {"Content-Type": "application/json"}), 200)
        self.assertEqual(self.req("POST", "/api/notes", json.dumps({"text": "hi2", "from": "owner", "project": "p"}),
                                  {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}), 200)
        self.assertEqual(self.req("GET", "/api/agents", host=f"localhost:{self.port}"), 200)

    def test_other_sites_and_rebound_names_refused(self):
        body = json.dumps({"to": "a1", "body": "rm -rf ~", "from": "owner"})
        self.assertEqual(self.req("POST", "/api/messages", body, {"Content-Type": "text/plain",
                                                                   "Origin": "https://evil.example"}), 403)
        self.assertEqual(self.req("POST", "/api/messages", body, {"Content-Type": "application/json",
                                                                   "Origin": "https://evil.example"}), 403)
        self.assertEqual(self.req("POST", "/api/messages", body, {"Content-Type": "text/plain"}), 403)  # form-like
        self.assertEqual(self.req("POST", "/api/messages", body, {"Content-Type": "application/json",
                                                                   "Origin": "null"}), 403)
        self.assertEqual(self.req("GET", "/api/agents", host=f"evil.example:{self.port}"), 403)  # DNS rebinding
        self.assertEqual(self.req("GET", "/", host=f"evil.example:{self.port}"), 403)
        self.assertFalse(self.h.s.q("SELECT 1 FROM messages WHERE body='rm -rf ~'"))

    def test_websocket_from_another_site_refused(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall((f"GET /ws/agents/a1?start=0 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: a2V5a2V5a2V5a2V5a2V5aw==\r\n"
                   "Sec-WebSocket-Version: 13\r\nOrigin: https://evil.example\r\n\r\n").encode())
        self.assertIn(b" 403 ", s.recv(4096).split(b"\r\n")[0])
        s.close()


def frame(payload, op=ws.BINARY, fin=True, masked=True, rsv=0, length=None):
    n = len(payload) if length is None else length
    head = bytes([(0x80 if fin else 0) | rsv | op])
    if n < 126:
        head += bytes([(0x80 if masked else 0) | n])
    elif n < 65536:
        head += bytes([(0x80 if masked else 0) | 126]) + struct.pack(">H", n)
    else:
        head += bytes([(0x80 if masked else 0) | 127]) + struct.pack(">Q", n)
    if not masked:
        return head + payload
    key = b"\x01\x02\x03\x04"
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class WebSocketFrameTests(unittest.TestCase):
    def recv(self, data):
        return ws.WebSocket(io.BytesIO(data), io.BytesIO()).recv()

    def test_browser_frames_read(self):
        self.assertEqual(self.recv(frame(b"hello")), (ws.BINARY, b"hello"))
        self.assertEqual(self.recv(frame(b"he", fin=False) + frame(b"x", op=ws.PING) + frame(b"llo", op=0)),
                         (ws.BINARY, b"hello"))  # a ping between fragments is fine

    def test_frames_a_browser_never_sends_are_refused(self):
        for bad in (frame(b"x", masked=False), frame(b"x", rsv=0x40), frame(b"x", op=0x3),
                    frame(b"x" * 200, op=ws.PING), frame(b"x", op=ws.PING, fin=False), frame(b"x", op=0),
                    frame(b"", length=ws.MAX_MESSAGE + 1), frame(b"", length=1 << 62),
                    frame(b"", fin=False) + frame(b"", op=0, fin=False) * ws.MAX_FRAGMENTS):
            with self.assertRaises(ConnectionError):
                self.recv(bad)


if __name__ == "__main__":
    unittest.main()
