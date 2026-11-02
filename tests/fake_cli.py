#!/usr/bin/env python3
"""A tiny stand-in for the interactive Claude/Codex TUI, for pty and browser tests.

Draws a '❯ ' composer on the bottom line, echoes keys, understands bracketed paste, Esc, Ctrl+C,
/exit, and SIGWINCH. On Enter it appends a user entry (and after FAKE_BUSY_S seconds an assistant
entry) to $FAKE_PROJECTS/fake/<session>.jsonl, like a real Claude transcript, and shows
'esc to interrupt' while "working". Writes its argv and account env to $FAKE_CLI_LOG (jsonl).
"""
import json
import os
import select
import signal
import sys
import termios
import time
import tty
import uuid
from datetime import datetime, timezone

args = sys.argv[1:]
sid = None
for flag in ("--resume", "--session-id"):  # a fork (--resume SRC --fork-session --session-id NEW) runs as NEW
    if flag in args:
        sid = args[args.index(flag) + 1]
if not sid and args[:1] == ["resume"] and len(args) > 1:
    sid = args[1]
sid = sid or str(uuid.uuid4())
BUSY_S = float(os.environ.get("FAKE_BUSY_S", "1.0"))
REPAINT_S = float(os.environ.get("FAKE_REPAINT_S", "0"))  # > 0: redraw the idle prompt this often, like a footer clock
proj = os.environ.get("FAKE_PROJECTS")
TRANSCRIPT = os.path.join(proj, "fake", sid + ".jsonl") if proj else None
if os.environ.get("FAKE_CLI_LOG"):
    with open(os.environ["FAKE_CLI_LOG"], "a") as f:
        f.write(json.dumps({"argv": sys.argv, "pid": os.getpid(), "cwd": os.getcwd(),
                            **{k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "SB_AGENT",
                                                              "TERM")}}) + "\n")

out = sys.stdout


def w(s):
    out.write(s)
    out.flush()


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def log(kind, text):
    if not TRANSCRIPT:
        return
    os.makedirs(os.path.dirname(TRANSCRIPT), exist_ok=True)
    with open(TRANSCRIPT, "a") as f:
        f.write(json.dumps({"type": kind, "timestamp": ts(), "sessionId": sid,
                            "message": {"role": kind, "content": [{"type": "text", "text": text}]}}) + "\n")


buf = ""
busy_until = 0.0
pending = None


def composer():
    w("\r\x1b[2K\x1b[1;32m❯\x1b[0m " + buf + ("" if buf else "\x1b[2mTry \"fix the tests\"\x1b[22m\r\x1b[2C"))


def size():
    s = os.get_terminal_size(0)
    return f"{s.columns}x{s.lines}"


def on_winch(*_):
    w(f"\r\x1b[2Ksize {size()}\r\n")
    composer()


signal.signal(signal.SIGWINCH, on_winch)
old = termios.tcgetattr(0)
tty.setraw(0)
w("\x1b[?2004h")
w(f"\x1b[1;38;2;134;194;50mFAKE CLI\x1b[0m {' '.join(args)}\r\nsession {sid} size {size()}\r\n")
composer()
paste = False
painted = time.time()
if os.environ.get("FAKE_TRUST_MENU"):  # a folder-trust choice instead of the prompt until Enter
    w("Do you trust the files in this folder?\r\n\x1b[1;32m❯\x1b[0m 1. Yes, I trust this folder\r\n  2. No, exit\r\n")
    while os.read(0, 16) not in (b"\r", b"\n"):
        pass
    w("\x1b[2J\x1b[H")
    composer()
try:
    while True:
        r, _, _ = select.select([0], [], [], 0.05)
        if REPAINT_S and not pending and time.time() - painted > REPAINT_S:
            painted = time.time()
            composer()
        if pending and time.time() >= busy_until:
            log("assistant", "ok: " + pending)
            w(f"\r\x1b[2K\x1b[1A\r\x1b[2K⏺ ok: {pending}\r\n")
            pending = None
            composer()
        if not r:
            continue
        data = os.read(0, 4096).decode(errors="replace")
        if not data:
            break
        i = 0
        while i < len(data):
            if data.startswith("\x1b[200~", i):
                paste, i = True, i + 6
                continue
            if data.startswith("\x1b[201~", i):
                paste, i = False, i + 6
                continue
            c = data[i]
            i += 1
            if paste:
                buf += c
                continue
            if c in "\r\n":
                text, buf = buf.strip(), ""
                if text == "/exit":
                    w("\r\n\x1b[2Kbye\r\n")
                    raise SystemExit(0)
                if text:
                    log("user", text)
                    w(f"\r\x1b[2K> {text}\r\n\x1b[2K✻ Working… (esc to interrupt)\r\n")
                    pending, busy_until = text, time.time() + BUSY_S
                composer()
            elif c == "\x1b":
                if pending:
                    w("\r\x1b[2K\x1b[1A\r\x1b[2KInterrupted\r\n")
                    pending = None
                composer()
            elif c == "\x03":
                buf = ""
                composer()
            elif c == "\x04":
                raise SystemExit(0)
            elif c in "\x7f\x08":
                buf = buf[:-1]
                composer()
            elif c >= " ":
                buf += c
                composer()
finally:
    termios.tcsetattr(0, termios.TCSADRAIN, old)
