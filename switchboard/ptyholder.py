"""One detached process per terminal agent: owns the pty and the CLI running in it, keeps ~2 MB of scrollback
(in memory and on disk) and serves it over a unix socket, so the hub can restart without killing the CLI.

Run: python3 -m switchboard.ptyholder <spec.json>
spec: {argv, cwd, env, sock, log, state, cols, rows}

Wire format both ways: 1 type byte + 4-byte big-endian length + payload.
  hub -> holder: b"i" input bytes, b"r" resize (json {cols, rows}), b"k" signal number (ascii) to the CLI's group
  holder -> hub: b"h" hello (json state), b"o" output bytes (first frame = scrollback replay), b"x" exit code (ascii)
"""
import errno
import fcntl
import json
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import termios
import time

KEEP = 2 * 1024 * 1024
BACKLOG = 4 * 1024 * 1024  # a client this far behind is dropped (a new one starts with up to KEEP of scrollback) ...
STALL_S = 10  # ... as is one that takes nothing for this long; it reconnects and gets the scrollback again


def frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


class FrameReader:
    """Incremental decoder for the wire format."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 5:
            n = struct.unpack(">I", self.buf[1:5])[0]
            if len(self.buf) < 5 + n:
                break
            out.append((bytes(self.buf[:1]), bytes(self.buf[5:5 + n])))
            del self.buf[:5 + n]
        return out


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(2, int(rows)), max(2, int(cols)), 0, 0))


def main(spec_path):
    spec = json.load(open(spec_path))
    os.unlink(spec_path)  # it carries the account environment
    log_path, sock_path, state_path = spec["log"], spec["sock"], spec["state"]
    scroll = bytearray()
    try:
        with open(log_path, "rb") as f:  # a relaunch (account switch) keeps the earlier screen history
            f.seek(0, 2)
            f.seek(max(0, f.tell() - KEEP))
            scroll += f.read()
    except OSError:
        pass
    master, slave = os.openpty()
    set_size(slave, spec.get("cols") or 120, spec.get("rows") or 40)

    def child_setup():
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(spec["argv"], cwd=spec.get("cwd") or os.path.expanduser("~"), env=spec["env"],
                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                            preexec_fn=child_setup, close_fds=True)
    os.close(slave)
    os.set_blocking(master, False)
    state = {"holder_pid": os.getpid(), "pid": proc.pid, "argv": spec["argv"], "cwd": spec.get("cwd"),
             "started_at": time.time(), "sock": sock_path, "agent": spec.get("agent"),
             "account": spec.get("account"), "session_id": spec.get("session_id")}
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(8)
    srv.setblocking(False)
    tmp = state_path + ".tmp"
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, state_path)

    logf = open(log_path, "ab")
    sel = selectors.DefaultSelector()
    sel.register(master, selectors.EVENT_READ, "pty")
    sel.register(srv, selectors.EVENT_READ, "srv")
    clients = {}  # sock -> FrameReader
    pending = {}  # sock -> [unsent output, last time it took some]; the loop never blocks on a client

    def drop(c):
        try:
            sel.unregister(c)
        except (KeyError, ValueError):
            pass
        clients.pop(c, None)
        pending.pop(c, None)
        c.close()

    def flush(c):
        out = pending[c]
        try:
            n = c.send(out[0])
        except BlockingIOError:
            n = 0
        except OSError:
            drop(c)
            return
        if n:
            del out[0][:n]
            out[1] = time.time()
        want = selectors.EVENT_READ | (selectors.EVENT_WRITE if out[0] else 0)
        if sel.get_key(c).events != want:
            sel.modify(c, want, "client")

    def send_all(data):
        for c in list(clients):
            out = pending[c]
            if not out[0]:
                out[1] = time.time()
            out[0] += data
            flush(c)

    def pace():
        """Drop a client that has fallen BACKLOG behind or taken nothing for STALL_S. The CLI never waits for a
        client, so one slow reader cannot hold up the others."""
        for c, out in list(pending.items()):
            if len(out[0]) > BACKLOG or (out[0] and time.time() - out[1] > STALL_S):
                drop(c)

    def write_input(data):
        while data:
            try:
                n = os.write(master, data)
                data = data[n:]
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                return

    signal.signal(signal.SIGTERM, lambda *_: os.killpg(proc.pid, signal.SIGTERM))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    eof = False
    while not eof:
        for key, mask in sel.select(timeout=1.0):
            if key.data == "pty":
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    continue
                except OSError as e:  # EIO: the CLI side closed
                    if e.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    eof = True
                    break
                scroll += data
                if len(scroll) > KEEP:
                    del scroll[:len(scroll) - KEEP]
                logf.write(data)
                logf.flush()
                if logf.tell() > 2 * KEEP:
                    logf.seek(0)
                    logf.truncate()
                    logf.write(scroll)
                    logf.flush()
                send_all(frame(b"o", data))
            elif key.data == "srv":
                try:
                    c, _ = srv.accept()
                except OSError:
                    continue
                c.setblocking(False)
                clients[c] = FrameReader()
                pending[c] = [bytearray(frame(b"h", json.dumps(state).encode()) + frame(b"o", bytes(scroll))),
                              time.time()]
                sel.register(c, selectors.EVENT_READ, "client")
                flush(c)
            else:
                c = key.fileobj
                if c in clients and mask & selectors.EVENT_WRITE:
                    flush(c)
                if c not in clients or not mask & selectors.EVENT_READ:
                    continue
                try:
                    data = c.recv(65536)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
                if not data:
                    drop(c)
                    continue
                for kind, payload in clients[c].feed(data):
                    if kind == b"i":
                        write_input(payload)
                    elif kind == b"r":
                        try:
                            sz = json.loads(payload)
                            set_size(master, sz["cols"], sz["rows"])
                        except (ValueError, KeyError, OSError):
                            pass
                    elif kind == b"k":
                        try:
                            os.killpg(proc.pid, int(payload))
                        except (OSError, ValueError):
                            pass
        if proc.poll() is not None and not eof:
            # drain whatever is left, then finish
            try:
                while True:
                    data = os.read(master, 65536)
                    if not data:
                        break
                    scroll += data
                    logf.write(data)
                    send_all(frame(b"o", data))
            except OSError:
                pass
            eof = True
        pace()
    code = proc.wait()
    # keep the on-disk scrollback bounded
    logf.close()
    try:
        if os.path.getsize(log_path) > 2 * KEEP:
            with open(log_path, "rb") as f:
                f.seek(-KEEP, 2)
                tail = f.read()
            with open(log_path, "wb") as f:
                f.write(tail)
    except OSError:
        pass
    state.update(exited_at=time.time(), exit_code=code)
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, state_path)
    send_all(frame(b"x", str(code).encode()))
    end = time.time() + STALL_S
    for c in list(clients):
        try:  # the last output and the exit code, to clients still reading
            c.settimeout(max(0.1, end - time.time()))
            c.sendall(pending[c][0])
        except OSError:
            pass
        drop(c)
    srv.close()
    try:
        os.unlink(sock_path)
    except OSError:
        pass


if __name__ == "__main__":
    main(sys.argv[1])
