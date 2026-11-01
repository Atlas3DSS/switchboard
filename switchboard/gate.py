"""The phone gate: the only way into the hub from outside its machine (your hostname -> a TLS reverse proxy ->
here; see docs/PHONE.md). Serves the pairing/login page; every other request needs a session made
by the paired phone (see switchboard.phone) and is then passed to the hub on 127.0.0.1 unchanged, WebSockets
included. Pairing tokens are only made on the hub's machine: /api/phone/* is never passed through.

  python3 -m switchboard.gate      (the phone address is set in Switchboard: Phone, or SB_GATE_ORIGIN)
"""
import http.client
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import phone

PORT = int(os.environ.get("SB_GATE_PORT", "8810"))
UPSTREAM = os.environ.get("SB_GATE_UPSTREAM", f"127.0.0.1:{os.environ.get('SB_PORT', '8802')}")
COOKIE = "sb_phone"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "cookie", "content-length"}
MAX_FAILS, FAIL_WINDOW_S = 20, 600
PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "gate.html")

_fails = []
_fails_lock = threading.Lock()


def _failed():
    with _fails_lock:
        _fails.append(time.time())


def _locked_out():
    with _fails_lock:
        cut = time.time() - FAIL_WINDOW_S
        _fails[:] = [t for t in _fails if t > cut]
        return len(_fails) >= MAX_FAILS


class Gate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "switchboard-gate"
    sys_version = ""

    def log_message(self, fmt, *args):  # no paths or tokens in the journal
        pass

    # ---------- helpers ----------
    def _send(self, code, body=b"", ctype="application/json", headers=()):
        if int(self.headers.get("Content-Length") or 0) and not getattr(self, "_body_read", False):
            headers = (*headers, ("Connection", "close"))  # refused before reading the body: never reuse this
            self.close_connection = True                    # connection, or the body would be read as a request
        self.send_response(code)
        for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                     ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                     ("X-Content-Type-Options", "nosniff"), *headers):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, headers=()):
        self._send(code, json.dumps(obj).encode(), headers=headers)

    def _sid(self):
        m = re.search(rf"(?:^|;\s*){COOKIE}=([\w-]+)", self.headers.get("Cookie") or "")
        return m.group(1) if m else None

    def _origin_ok(self):
        return bool(phone.origin()) and self.headers.get("Origin") == phone.origin()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._body_read = True
        return self.rfile.read(n) if n else b""

    # ---------- routing ----------
    def _route(self):
        self._body_read = False
        path = urlparse(self.path).path
        if path in ("/gate", "/gate/"):
            return self._send(200, open(PAGE, "rb").read(), "text/html; charset=utf-8",
                              (("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                                "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"),))
        if path.startswith("/gate/"):
            return self._gate_api(path)
        if path.startswith("/api/phone"):
            return self._json(403, {"error": "pairing is managed on the hub's machine only"})
        if not phone.session_ok(self._sid()):
            if self.command in ("GET", "HEAD") and not path.startswith(("/api/", "/ws/")):
                return self._send(302, headers=(("Location", "/gate/"),))
            return self._json(401, {"error": "sign in from the paired phone"}, (("X-Gate", "login"),))
        if self.command not in ("GET", "HEAD") and not self._origin_ok():
            return self._json(403, {"error": "bad origin"})
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            if not self._origin_ok():
                return self._json(403, {"error": "bad origin"})
            return self._tunnel()
        return self._proxy()

    def _gate_api(self, path):
        if _locked_out():
            return self._json(429, {"error": "too many failed attempts; wait ten minutes"})
        if path == "/gate/status" and self.command == "GET":
            return self._json(200, {"paired": phone.status()["paired"], "signed_in": phone.session_ok(self._sid())})
        if self.command != "POST" or not self._origin_ok():
            return self._json(403, {"error": "bad request"})
        try:
            b = json.loads(self._body() or b"{}")
            if path == "/gate/challenge":
                return self._json(200, {"nonce": phone.challenge()})
            if path == "/gate/pair":
                phone.pair(b.get("token"), b.get("spki"), self.headers.get("User-Agent") or "")
                return self._json(200, {"ok": True})
            if path == "/gate/login":
                sid, ttl = phone.login(b.get("nonce"), b.get("sig"), phone.origin())
                cookie = f"{COOKIE}={sid}; Max-Age={ttl}; Path=/; HttpOnly; Secure; SameSite=Strict"
                return self._json(200, {"ok": True}, (("Set-Cookie", cookie),))
        except PermissionError as e:
            _failed()
            return self._json(403, {"error": str(e)})
        except (ValueError, TypeError) as e:
            _failed()
            return self._json(400, {"error": f"bad request: {e}"})
        return self._json(404, {"error": "not found"})

    # ---------- passing requests on to the hub ----------
    def _upstream_headers(self, keep_hop=False):
        """The request's headers for the hub: no session cookie, and the gate's own origin in place of the phone's
        (already checked here), so the hub's same-origin check sees a local page."""
        drop = {"host", "cookie"} if keep_hop else HOP
        h = {k: v for k, v in self.headers.items() if k.lower() not in drop and k.lower() != "origin"}
        if self.headers.get("Origin"):
            h["Origin"] = f"http://{UPSTREAM}"
        return h

    def _proxy(self):
        body = self._body()
        host, port = UPSTREAM.rsplit(":", 1)
        conn = http.client.HTTPConnection(host, int(port), timeout=120)
        try:
            conn.request(self.command, self.path, body=body or None, headers=self._upstream_headers())
            r = conn.getresponse()
            data = r.read()
        except OSError:
            return self._json(502, {"error": "the hub is not answering (restarting?)"})
        finally:
            conn.close()
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _tunnel(self):
        """A WebSocket: replay the handshake to the hub, then copy bytes both ways until either side closes."""
        host, port = UPSTREAM.rsplit(":", 1)
        try:
            up = socket.create_connection((host, int(port)), timeout=10)
        except OSError:
            return self._json(502, {"error": "the hub is not answering (restarting?)"})
        up.settimeout(None)
        lines = [f"{self.command} {self.path} HTTP/1.1", f"Host: {UPSTREAM}"]
        lines += [f"{k}: {v}" for k, v in self._upstream_headers(keep_hop=True).items()]
        up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        down = self.connection
        self.close_connection = True

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=pump, args=(up, down), daemon=True)
        t.start()
        pump(down, up)
        t.join(5)
        up.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = lambda self: self._route()


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Gate)
    srv.daemon_threads = True
    print(f"switchboard gate on 127.0.0.1:{PORT} -> {UPSTREAM} (phone address {phone.origin() or 'not set yet'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
