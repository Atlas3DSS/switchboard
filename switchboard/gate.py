"""The phone gate: the only way into the hub from outside its machine (your hostname -> a TLS reverse proxy ->
here; see docs/PHONE.md). Serves the pairing/login page; every other request needs a session made
by the paired phone (see switchboard.phone) and is then passed to the hub on 127.0.0.1 unchanged, WebSockets
included. Pairing tokens are only made on the hub's machine: /api/phone/* is never passed through.

Limits: small bodies before sign-in, bounded connections and read times, failure and challenge limits per client
(never global, so strangers cannot lock the phone out), and open terminal streams are cut within seconds of an
unpair, a new pairing or the session's expiry.

  python3 -m switchboard.gate      (the phone address is set in Switchboard: Phone, or SB_GATE_ORIGIN)
"""
import http.client
import ipaddress
import json
import os
import re
import socket
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import phone

PORT = int(os.environ.get("SB_GATE_PORT", "8810"))
UPSTREAM = os.environ.get("SB_GATE_UPSTREAM", f"127.0.0.1:{os.environ.get('SB_PORT', '8802')}")
COOKIE = "sb_phone"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "cookie", "content-length"}
MAX_FAILS, MAX_CHALLENGES, WINDOW_S = 20, 30, 600  # per client and window
MAX_AUTH_BODY = 4096          # pairing / sign-in requests
MAX_PROXY_BODY = 16 << 20     # signed-in requests (image uploads are capped at 15 MB by the hub)
MAX_CONNECTIONS = 64
READ_TIMEOUT_S = 30           # silence while reading, and the whole request line + headers; streams have none
AUTH_BODY_S, PROXY_BODY_S = 10, 300  # a whole body must arrive within this
REVALIDATE_S = 2              # how often an open terminal stream re-checks its session
PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "gate.html")


class Limits:
    """Recent events per (client, kind), for a bounded number of clients (the least recently seen give way), each
    keeping no more events than the largest limit it is checked against."""
    def __init__(self, clients=1024, per_key=max(MAX_FAILS, MAX_CHALLENGES)):
        self.clients, self.per_key, self.seen, self.lock = clients, per_key, OrderedDict(), threading.Lock()

    def _q(self, key):
        q = self.seen.pop(key, None) or deque(maxlen=self.per_key)
        self.seen[key] = q
        while len(self.seen) > self.clients:
            self.seen.popitem(last=False)
        cut = time.time() - WINDOW_S
        while q and q[0] < cut:
            q.popleft()
        return q

    def over(self, client, kind, limit):
        with self.lock:
            return len(self._q((client, kind))) >= limit

    def hit(self, client, kind):
        with self.lock:
            self._q((client, kind)).append(time.time())


limits = Limits()


class TooLarge(ValueError):
    pass


class TooSlow(ValueError):
    pass


class Gate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "switchboard-gate"
    sys_version = ""
    timeout = READ_TIMEOUT_S

    def log_message(self, fmt, *args):  # no paths or tokens in the journal
        pass

    def handle_one_request(self):
        """The request line and headers must all arrive within READ_TIMEOUT_S, however slowly they trickle in."""
        timer = threading.Timer(READ_TIMEOUT_S, self._hang_up)
        self._header_timer = timer
        timer.daemon = True
        timer.start()
        try:
            super().handle_one_request()
        finally:
            timer.cancel()

    def parse_request(self):
        ok = super().parse_request()
        self._header_timer.cancel()
        return ok and not getattr(self, "_hung_up", False)  # headers "ended" only because we cut the line

    def _hang_up(self):
        self._hung_up = self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # ---------- helpers ----------
    def _send(self, code, body=b"", ctype="application/json", headers=()):
        if self.headers.get("Content-Length", "0") != "0" and not getattr(self, "_body_read", False) \
                or self.headers.get("Transfer-Encoding"):
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

    def _client(self):
        """Who is asking: the address our own reverse proxy put last in X-Forwarded-For (anything before it came from
        the client), trusted only when the request arrived from loopback; else the socket's peer."""
        peer = self.client_address[0]
        if ipaddress.ip_address(peer).is_loopback:
            last = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            try:
                return str(ipaddress.ip_address(last))
            except ValueError:
                pass
        return peer

    def _body(self, limit, seconds):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("send a Content-Length body")
        lengths = self.headers.get_all("Content-Length") or ["0"]
        n = lengths[0].strip()
        if len(lengths) > 1 or not n.isdigit():
            raise ValueError("bad Content-Length")
        n = int(n)
        if n > limit:
            raise TooLarge(f"request body over {limit} bytes")
        data, deadline = b"", time.monotonic() + seconds
        try:
            while len(data) < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TooSlow("request body too slow")
                self.connection.settimeout(min(left, READ_TIMEOUT_S))  # no single read outlasts the whole budget
                try:
                    chunk = self.rfile.read1(min(65536, n - len(data)))
                except (TimeoutError, socket.timeout):
                    raise TooSlow("request body too slow")
                if not chunk:
                    raise TooSlow("request body cut short")
                data += chunk
            if time.monotonic() > deadline:
                raise TooSlow("request body too slow")
        finally:
            self.connection.settimeout(READ_TIMEOUT_S)
        self._body_read = True
        return data

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
        sid = self._sid()
        if not phone.session_ok(sid):
            if self.command in ("GET", "HEAD") and not path.startswith(("/api/", "/ws/")):
                return self._send(302, headers=(("Location", "/gate/"),))
            return self._json(401, {"error": "sign in from the paired phone"}, (("X-Gate", "login"),))
        if self.command not in ("GET", "HEAD") and not self._origin_ok():
            return self._json(403, {"error": "bad origin"})
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            if not self._origin_ok():
                return self._json(403, {"error": "bad origin"})
            return self._tunnel(sid)
        return self._proxy()

    def _gate_api(self, path):
        if path == "/gate/status" and self.command == "GET":
            return self._json(200, {"paired": phone.status()["paired"], "signed_in": phone.session_ok(self._sid())})
        if self.command != "POST" or not self._origin_ok():
            return self._json(403, {"error": "bad request"})
        client = self._client()
        try:
            b = json.loads(self._body(MAX_AUTH_BODY, AUTH_BODY_S) or b"{}")
            if not isinstance(b, dict):
                raise ValueError("expected a JSON object")
            if path == "/gate/challenge":
                if limits.over(client, "challenge", MAX_CHALLENGES):
                    return self._json(429, {"error": "too many sign-in attempts; wait a few minutes"})
                limits.hit(client, "challenge")
                return self._json(200, {"nonce": phone.challenge(client)})
            if path == "/gate/pair":
                if limits.over(client, "fail", MAX_FAILS):
                    return self._json(429, {"error": "too many failed attempts; wait ten minutes"})
                phone.pair(b.get("token"), b.get("spki"), self.headers.get("User-Agent") or "")
                return self._json(200, {"ok": True})
            if path == "/gate/login":  # every attempt uses up a challenge, so a valid signature is never refused
                sid, ttl = phone.login(b.get("nonce"), b.get("sig"), phone.origin())
                cookie = f"{COOKIE}={sid}; Max-Age={ttl}; Path=/; HttpOnly; Secure; SameSite=Strict"
                return self._json(200, {"ok": True}, (("Set-Cookie", cookie),))
        except TooLarge as e:
            return self._json(413, {"error": str(e)})
        except TooSlow as e:
            return self._json(408, {"error": str(e)})
        except phone.Busy as e:
            return self._json(429, {"error": str(e)})
        except PermissionError as e:
            limits.hit(client, "fail")
            return self._json(403, {"error": str(e)})
        except (ValueError, TypeError) as e:
            limits.hit(client, "fail")
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
        try:
            body = self._body(MAX_PROXY_BODY, PROXY_BODY_S)
        except TooLarge as e:
            return self._json(413, {"error": str(e)})
        except TooSlow as e:
            return self._json(408, {"error": str(e)})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
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

    def _tunnel(self, sid):
        """A WebSocket: replay the handshake to the hub, then copy bytes both ways until either side closes, or until
        the session that opened it ends (unpaired, replaced by a new pairing, expired)."""
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
        down.settimeout(None)
        self.close_connection = True
        done = threading.Event()

        def cut():
            done.set()
            for s in (up, down):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

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
                cut()

        def watch():
            while not done.wait(REVALIDATE_S):
                if not phone.session_ok(sid):
                    cut()

        t = threading.Thread(target=pump, args=(up, down), daemon=True)
        t.start()
        threading.Thread(target=watch, daemon=True).start()
        pump(down, up)
        t.join(5)
        up.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = lambda self: self._route()


class Server(ThreadingHTTPServer):
    """At most MAX_CONNECTIONS connections at a time; more are closed at once instead of queuing threads."""
    daemon_threads = True

    def __init__(self, *a, **kw):
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):  # a client hanging up is not news
            super().handle_error(request, client_address)


def main():
    srv = Server(("127.0.0.1", PORT), Gate)
    print(f"switchboard gate on 127.0.0.1:{PORT} -> {UPSTREAM} (phone address {phone.origin() or 'not set yet'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
