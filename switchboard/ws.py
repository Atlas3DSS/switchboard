"""Minimal RFC 6455 WebSocket server side for the stdlib HTTP handler (no extensions, no compression)."""
import base64
import hashlib
import socket
import struct
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 8 << 20  # one browser message (keys, a paste, a resize); terminal output only flows the other way
MAX_FRAGMENTS = 1024


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


class WebSocket:
    def __init__(self, rfile, wfile, sock=None):
        self.r, self.w, self.sock = rfile, wfile, sock
        self.lock = threading.Lock()
        self.closed = False
        self.sending_since = 0.0  # when the send in progress started, or 0

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.r.read(n - len(data))
            if not chunk:
                raise ConnectionError("websocket closed")
            data += chunk
        return data

    def _fail(self, why):
        self.close()
        raise ConnectionError(f"websocket protocol error: {why}")

    def recv(self):
        """Next complete message as (opcode, payload); answers pings itself. Raises ConnectionError on close or on
        a frame a browser would never send (unmasked, reserved bits or opcodes, oversized)."""
        parts, op0, size = [], None, 0
        while True:
            b1, b2 = self._read(2)
            fin, op, masked, n = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if b1 & 0x70 or op not in (0, TEXT, BINARY, CLOSE, PING, PONG):
                self._fail("reserved bits or opcode")
            if not masked:
                self._fail("client frames must be masked")
            if op >= CLOSE and (n > 125 or not fin):
                self._fail("bad control frame")
            if op < CLOSE and (op == 0) != (op0 is not None):  # control frames may come between fragments
                self._fail("continuation out of order")
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            size += n if op < CLOSE else 0
            if size > MAX_MESSAGE or len(parts) >= MAX_FRAGMENTS:
                self._fail("message too large")
            mask = self._read(4)
            data = bytearray(self._read(n))
            for i in range(n):
                data[i] ^= mask[i % 4]
            data = bytes(data)
            if op == CLOSE:
                self.close()
                raise ConnectionError("websocket closed by peer")
            if op == PING:
                self.send(data, PONG)
                continue
            if op == PONG:
                continue
            if op != 0:
                op0 = op
            parts.append(data)
            if fin:
                return op0, b"".join(parts)

    def send(self, data, opcode=None):
        if isinstance(data, str):
            data, opcode = data.encode(), opcode or TEXT
        opcode = opcode or BINARY
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        with self.lock:
            if self.closed and opcode != CLOSE:
                raise ConnectionError("websocket closed")
            self.sending_since = time.time()
            try:
                self.w.write(head + data)
                self.w.flush()
            finally:
                self.sending_since = 0.0

    def abort(self):
        """End the connection now, even while a send or recv is blocked on it (a peer that stopped reading)."""
        self.closed = True
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        if self.closed:
            return
        try:
            self.send(b"\x03\xe8", CLOSE)
        except (OSError, ConnectionError):
            pass
        self.closed = True
