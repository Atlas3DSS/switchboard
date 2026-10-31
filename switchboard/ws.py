"""Minimal RFC 6455 WebSocket server side for the stdlib HTTP handler (no extensions, no compression)."""
import base64
import hashlib
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


class WebSocket:
    def __init__(self, rfile, wfile):
        self.r, self.w = rfile, wfile
        self.lock = threading.Lock()
        self.closed = False

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.r.read(n - len(data))
            if not chunk:
                raise ConnectionError("websocket closed")
            data += chunk
        return data

    def recv(self):
        """Next complete message as (opcode, payload); answers pings itself. Raises ConnectionError on close."""
        parts, op0 = [], None
        while True:
            b1, b2 = self._read(2)
            fin, op, masked, n = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else b"\0\0\0\0"
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
            self.w.write(head + data)
            self.w.flush()

    def close(self):
        if self.closed:
            return
        try:
            self.send(b"\x03\xe8", CLOSE)
        except (OSError, ConnectionError):
            pass
        self.closed = True
