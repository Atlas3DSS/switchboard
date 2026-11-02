"""Phone pairing and the gate: one phone at a time, single-use codes and challenges, a session only from the
paired key, and a gate that lets nothing through without one. Run: python3 -m unittest tests.test_phone
"""
import base64
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbphone-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ.pop("SB_GATE_ORIGIN", None)

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402

from switchboard import gate, phone  # noqa: E402

ORIGIN = "https://sb.example.com"


class Phone:
    """What the phone's browser does: a P-256 key, the public half sent as SPKI, raw r||s signatures."""
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.spki = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def sign(self, nonce, origin=ORIGIN):
        r, s = decode_dss_signature(self.key.sign(phone.login_message(nonce, origin), ec.ECDSA(hashes.SHA256())))
        return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


class PairingTests(unittest.TestCase):
    def setUp(self):
        phone.unpair()
        phone.set_origin(ORIGIN)

    def test_pair_login_and_only_one_phone(self):
        a, b = Phone(), Phone()
        token, _ = phone.new_pairing()
        phone.pair(token, a.spki, "phone A")
        with self.assertRaises(PermissionError):  # the code works once
            phone.pair(token, b.spki)
        n = phone.challenge()
        sid, _ = phone.login(n, a.sign(n), ORIGIN)
        self.assertTrue(phone.session_ok(sid))
        with self.assertRaises(PermissionError):  # the challenge works once
            phone.login(n, a.sign(n), ORIGIN)
        n = phone.challenge()
        with self.assertRaises(PermissionError):  # another key cannot sign in
            phone.login(n, b.sign(n), ORIGIN)
        n = phone.challenge()
        with self.assertRaises(PermissionError):  # a signature made for another site does not count
            phone.login(n, a.sign(n, "https://evil.example"), ORIGIN)
        token, _ = phone.new_pairing()  # pairing B replaces A and ends A's session
        phone.pair(token, b.spki)
        self.assertFalse(phone.session_ok(sid))
        n = phone.challenge()
        with self.assertRaises(PermissionError):
            phone.login(n, a.sign(n), ORIGIN)
        n = phone.challenge()
        self.assertTrue(phone.session_ok(phone.login(n, b.sign(n), ORIGIN)[0]))

    def test_a_failed_sign_in_uses_up_its_challenge(self):
        a = Phone()
        token, _ = phone.new_pairing()
        phone.pair(token, a.spki)
        for bad in (Phone().sign, lambda n: "not base64!", lambda n: ""):
            n = phone.challenge()
            with self.assertRaises(PermissionError):
                phone.login(n, bad(n), ORIGIN)
            with self.assertRaises(PermissionError):  # the same challenge no longer works, even signed right
                phone.login(n, a.sign(n), ORIGIN)

    def test_codes_expire_and_unpair_signs_out(self):
        a = Phone()
        old, _ = phone.new_pairing()
        token, _ = phone.new_pairing()  # a new code cancels the unused one
        with self.assertRaises(PermissionError):
            phone.pair(old, a.spki)
        real = time.time
        try:
            time.time = lambda: real() + phone.PAIR_TTL_S + 1
            with self.assertRaises(PermissionError):
                phone.pair(token, a.spki)
        finally:
            time.time = real
        token, _ = phone.new_pairing()
        phone.pair(token, a.spki)
        n = phone.challenge()
        sid, _ = phone.login(n, a.sign(n), ORIGIN)
        phone.unpair()
        self.assertFalse(phone.session_ok(sid))
        with self.assertRaises(PermissionError):
            phone.challenge()
        self.assertEqual(os.stat(phone.PATH).st_mode & 0o777, 0o600)
        self.assertNotIn(sid, open(phone.PATH).read())  # only hashes on disk

    def test_rejects_other_key_types_and_bad_addresses(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        rsa_spki = base64.b64encode(rsa.generate_private_key(65537, 2048).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        token, _ = phone.new_pairing()
        with self.assertRaises(ValueError):
            phone.pair(token, rsa_spki)
        for bad in ("http://sb.example.com", "sb.example.com", "https://x.com/path", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                phone.set_origin(bad)


class Upstream(BaseHTTPRequestHandler):
    """Stands in for the hub: echoes what reached it."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _echo(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.dumps({"path": self.path, "method": self.command, "cookie": self.headers.get("Cookie"),
                           "body": self.rfile.read(n).decode() if n else ""}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Upgrade") == "websocket":  # answer the handshake, then echo bytes
            self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            self.wfile.flush()
            try:
                while True:  # echo until either side closes
                    data = self.connection.recv(100)
                    if not data:
                        break
                    self.connection.sendall(b"echo:" + data)
            except OSError:
                pass
            self.close_connection = True
            return
        self._echo()

    do_POST = _echo


def serve(handler, server=ThreadingHTTPServer):
    srv = server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = serve(Upstream)
        gate.UPSTREAM = f"127.0.0.1:{cls.up.server_port}"
        cls.gw = serve(gate.Gate, gate.Server)

    def setUp(self):
        phone.unpair()
        phone.set_origin(ORIGIN)
        gate.limits = gate.Limits()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.gw.server_port, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def signed_in(self):
        a = Phone()
        token, _ = phone.new_pairing()
        o = {"Origin": ORIGIN, "Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/gate/pair", {"token": token, "spki": a.spki}, o)[0].status, 200)
        n = json.loads(self.req("POST", "/gate/challenge", {}, o)[1])["nonce"]
        r, _ = self.req("POST", "/gate/login", {"nonce": n, "sig": a.sign(n)}, o)
        self.assertEqual(r.status, 200)
        cookie = r.getheader("Set-Cookie")
        for flag in ("HttpOnly", "Secure", "SameSite=Strict"):
            self.assertIn(flag, cookie)
        return cookie.split(";")[0]

    def test_nothing_passes_without_a_session(self):
        r, _ = self.req("GET", "/")
        self.assertEqual((r.status, r.getheader("Location")), (302, "/gate/"))
        r, _ = self.req("GET", "/api/overview")
        self.assertEqual((r.status, r.getheader("X-Gate")), (401, "login"))
        r, _ = self.req("GET", "/api/overview", headers={"Cookie": "sb_phone=forged"})
        self.assertEqual(r.status, 401)
        r, body = self.req("GET", "/gate/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"switchboard-phone-login", body)
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        r, _ = self.req("POST", "/gate/challenge", {}, {"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)

    def test_session_passes_through_but_never_to_pairing(self):
        cookie = self.signed_in()
        r, body = self.req("GET", "/api/overview?x=1", headers={"Cookie": cookie})
        self.assertEqual(r.status, 200)
        seen = json.loads(body)
        self.assertEqual(seen["path"], "/api/overview?x=1")
        self.assertIsNone(seen["cookie"])  # the session cookie stays at the gate
        r, _ = self.req("POST", "/api/messages", {"to": "x"}, {"Cookie": cookie, "Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)  # cross-site writes refused
        r, body = self.req("POST", "/api/messages", {"to": "x"}, {"Cookie": cookie, "Origin": ORIGIN})
        self.assertEqual(json.loads(body)["body"], '{"to": "x"}')
        for p in ("/api/phone", "/api/phone/pair", "/api/phone/unpair"):
            self.assertEqual(self.req("POST", p, {}, {"Cookie": cookie, "Origin": ORIGIN})[0].status, 403)

    def ws(self, cookie, origin=ORIGIN):
        s = socket.create_connection(("127.0.0.1", self.gw.server_port), timeout=5)
        s.sendall((f"GET /ws/agents/x HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: a2V5a2V5a2V5a2V5a2V5aw==\r\nSec-WebSocket-Version: 13\r\n"
                   f"Origin: {origin}\r\nCookie: {cookie}\r\n\r\n").encode())
        return s, s.recv(4096)

    def closes_soon(self, s):
        """True if the gate cuts this open stream within a few revalidation periods."""
        s.settimeout(gate.REVALIDATE_S * 3)
        try:
            for _ in range(20):
                s.sendall(b"x")
                if not s.recv(100):
                    return True
                time.sleep(0.3)
        except OSError:
            return True
        return False

    def test_websocket_through_the_gate(self):
        cookie = self.signed_in()
        ws = lambda origin: self.ws(cookie, origin)
        s, head = ws("https://evil.example")
        self.assertIn(b" 403 ", head.split(b"\r\n")[0])
        s.close()
        s, head = ws(ORIGIN)
        self.assertIn(b" 101 ", head.split(b"\r\n")[0])
        s.sendall(b"ping")
        self.assertEqual(s.recv(100), b"echo:ping")
        s.close()

    def test_refused_body_never_leaks_into_the_next_request(self):
        c = http.client.HTTPConnection("127.0.0.1", self.gw.server_port, timeout=10)
        c.request("POST", "/api/messages", body=json.dumps({"to": "x", "body": "hi"}),
                  headers={"Content-Type": "application/json", "Origin": ORIGIN})
        r = c.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Connection")), (401, "close"))
        c.request("GET", "/gate/")  # http.client opens a fresh connection after "close"
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        r.read()
        c.close()

    def test_open_stream_ends_with_its_session(self):
        cookie = self.signed_in()
        s, head = self.ws(cookie)
        self.assertIn(b" 101 ", head.split(b"\r\n")[0])
        s.sendall(b"hi")
        self.assertEqual(s.recv(100), b"echo:hi")
        phone.unpair()
        self.assertTrue(self.closes_soon(s), "stream stayed open after unpair")
        s.close()
        cookie = self.signed_in()  # pairing another phone ends the first one's streams too
        s, _ = self.ws(cookie)
        token, _ = phone.new_pairing()
        phone.pair(token, Phone().spki)
        self.assertTrue(self.closes_soon(s), "stream stayed open after another phone was paired")
        s.close()
        cookie = self.signed_in()  # and when the session that opened it expires
        s, _ = self.ws(cookie)
        s.sendall(b"hi")
        self.assertEqual(s.recv(100), b"echo:hi")
        with phone._state() as st:
            for v in st["sessions"].values():
                v["expires"] = time.time() - 1
        self.assertTrue(self.closes_soon(s), "stream stayed open after its session expired")
        s.close()

    def test_failures_lock_out_that_client_only(self):
        o = lambda ip: {"Origin": ORIGIN, "Content-Type": "application/json", "X-Forwarded-For": ip}
        for _ in range(gate.MAX_FAILS):
            self.req("POST", "/gate/pair", {"token": "guess", "spki": Phone().spki}, o("203.0.113.5"))
        self.assertEqual(self.req("POST", "/gate/pair", {"token": "guess", "spki": Phone().spki},
                                  o("203.0.113.5"))[0].status, 429)
        # the client is the address our proxy appended last; a forged earlier entry does not dodge the limit
        self.assertEqual(self.req("POST", "/gate/pair", {"token": "guess", "spki": Phone().spki},
                                  o("198.51.100.7, 203.0.113.5"))[0].status, 429)
        a = Phone()  # the real phone, elsewhere, pairs and signs in while that client is locked out
        token, _ = phone.new_pairing()
        self.assertEqual(self.req("POST", "/gate/pair", {"token": token, "spki": a.spki}, o("192.0.2.9"))[0].status, 200)
        n = json.loads(self.req("POST", "/gate/challenge", {}, o("192.0.2.9"))[1])["nonce"]
        self.assertEqual(self.req("POST", "/gate/login", {"nonce": n, "sig": a.sign(n)}, o("192.0.2.9"))[0].status, 200)
        n = json.loads(self.req("POST", "/gate/challenge", {}, o("203.0.113.5"))[1])["nonce"]  # even from there
        self.assertEqual(self.req("POST", "/gate/login", {"nonce": n, "sig": a.sign(n)}, o("203.0.113.5"))[0].status, 200)

    def test_requests_are_bounded(self):
        a = Phone()
        token, _ = phone.new_pairing()
        phone.pair(token, a.spki)
        o = {"Origin": ORIGIN, "Content-Type": "application/json"}
        r, _ = self.req("POST", "/gate/pair", {"token": "x" * (gate.MAX_AUTH_BODY + 10)}, o)
        self.assertEqual((r.status, r.getheader("Connection")), (413, "close"))
        c = http.client.HTTPConnection("127.0.0.1", self.gw.server_port, timeout=10)
        c.putrequest("POST", "/gate/challenge")
        for k, v in {**o, "Transfer-Encoding": "chunked"}.items():
            c.putheader(k, v)
        c.endheaders()
        c.send(b"2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(c.getresponse().status, 400)
        c.close()
        for _ in range(gate.MAX_CHALLENGES):
            self.req("POST", "/gate/challenge", {}, o)
        self.assertEqual(self.req("POST", "/gate/challenge", {}, o)[0].status, 429)
        for _ in range(20):
            phone.challenge("203.0.113.5")
        mine = [v for v in json.load(open(phone.PATH))["challenges"].values() if v["client"] == "203.0.113.5"]
        self.assertEqual(len(mine), phone.MAX_CHALLENGES_PER_CLIENT)  # a client only pushes out its own
        lengths = {"Origin": ORIGIN, "Content-Type": "application/json"}
        c = http.client.HTTPConnection("127.0.0.1", self.gw.server_port, timeout=10)
        c.putrequest("POST", "/gate/challenge")
        for k, v in lengths.items():
            c.putheader(k, v)
        c.putheader("Content-Length", "2")
        c.putheader("Content-Length", "40")
        c.endheaders()
        c.send(b"{}")
        self.assertEqual(c.getresponse().status, 400)  # two lengths: refused
        c.close()
        lim = gate.Limits()
        for _ in range(10000):
            lim.hit("x", "fail")
        self.assertEqual(len(lim.seen[("x", "fail")]), lim.per_key)  # bounded per client too

    def test_phone_signs_in_while_others_churn_challenges(self):
        a = Phone()
        token, _ = phone.new_pairing()
        phone.pair(token, a.spki)
        o = lambda ip: {"Origin": ORIGIN, "Content-Type": "application/json", "X-Forwarded-For": ip}
        n = json.loads(self.req("POST", "/gate/challenge", {}, o("192.0.2.9"))[1])["nonce"]  # the phone's
        for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):  # 3 strangers, each up to its own limit
            for _ in range(gate.MAX_CHALLENGES):
                self.req("POST", "/gate/challenge", {}, o(ip))
        self.assertEqual(self.req("POST", "/gate/login", {"nonce": n, "sig": a.sign(n)}, o("192.0.2.9"))[0].status, 200)
        old = phone.MAX_CHALLENGES
        phone.MAX_CHALLENGES = len(json.load(open(phone.PATH))["challenges"])
        try:  # a full table refuses new challenges instead of dropping anyone's
            self.assertEqual(self.req("POST", "/gate/challenge", {}, o("198.51.100.1"))[0].status, 429)
        finally:
            phone.MAX_CHALLENGES = old

    def test_slow_requests_are_cut(self):
        old = gate.READ_TIMEOUT_S, gate.AUTH_BODY_S
        gate.READ_TIMEOUT_S, gate.AUTH_BODY_S = 1.0, 1.0
        try:
            s = socket.create_connection(("127.0.0.1", self.gw.server_port), timeout=5)
            s.sendall(b"GET /gate/ HTTP/1.1\r\n")
            t0, closed = time.time(), False
            while time.time() - t0 < 4:  # headers trickle in, never finishing
                try:
                    s.sendall(b"X-Slow: 1\r\n")
                except OSError:
                    closed = True
                    break
                s.settimeout(0.3)
                try:
                    if s.recv(100) == b"":
                        closed = True
                        break
                except socket.timeout:
                    pass
            self.assertTrue(closed and time.time() - t0 < 3, "slow headers were not cut")
            s.close()
            s = socket.create_connection(("127.0.0.1", self.gw.server_port), timeout=5)
            s.sendall(f"POST /gate/challenge HTTP/1.1\r\nHost: h\r\nOrigin: {ORIGIN}\r\n"
                      f"Content-Type: application/json\r\nContent-Length: 50\r\n\r\n{{".encode())
            for _ in range(3):
                time.sleep(0.6)
                try:
                    s.sendall(b" ")
                except OSError:
                    break
            s.settimeout(3)
            self.assertIn(b" 408 ", s.recv(4096).split(b"\r\n")[0])  # the body never finished in time
            s.close()
        finally:
            gate.READ_TIMEOUT_S, gate.AUTH_BODY_S = old

    def test_connections_are_bounded(self):
        old = gate.MAX_CONNECTIONS
        gate.MAX_CONNECTIONS = 2
        try:
            srv = serve(gate.Gate, gate.Server)
        finally:
            gate.MAX_CONNECTIONS = old
        idle = [socket.create_connection(("127.0.0.1", srv.server_port), timeout=5) for _ in range(2)]
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", srv.server_port), timeout=5)
        self.assertEqual(extra.recv(10), b"")  # closed at once instead of queued
        for s in idle + [extra]:
            s.close()
        srv.shutdown()


if __name__ == "__main__":
    unittest.main()
