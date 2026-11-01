"""Terminal agents: each one is the interactive CLI (claude / codex) in a pty owned by a detached ptyholder
process. The hub keeps one connection per running holder: it mirrors the screen (vtscreen) to see busy/idle and
whether the composer is empty, fans the output out to browser WebSockets, and types `sb send` messages in exactly
once, confirming each one from the session transcript.
"""
import fcntl
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

from . import discovery, shared
from .ptyholder import KEEP, FrameReader, frame
from .store import STATE, epoch, iso_from_epoch, now
from .vtscreen import Screen

PTY_DIR = os.path.join(STATE, "pty")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIRM_S = 180  # a typed message must show up in the transcript within this long


def cli_size(pid):
    """(cols, rows) of the CLI's own terminal, or None. Read from the pty it has as stdin."""
    try:
        fd = os.open(f"/proc/{int(pid)}/fd/0", os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except (OSError, TypeError, ValueError):
        return None
    try:
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
    except OSError:
        return None
    finally:
        os.close(fd)
    return (cols, rows) if cols > 1 and rows > 1 else None
EXIT_WAIT_S = 30


PROVIDERS = ("claude", "codex", "gemini", "hermes", "api", "shell")
SB_BIN = os.path.join(ROOT, "bin")


def cli_bin(provider):
    return os.environ.get(f"SB_{provider.upper()}_BIN") or provider


def has_history(agent):
    sid = agent.get("session_id")
    if not sid:
        return False
    return bool(discovery.transcript_path(agent["provider"], sid))


def launch_argv(agent):
    """The interactive command line for an agent: resume its session if it has one on disk, else start it."""
    if agent["provider"] == "shell":
        return [os.environ.get("SB_SHELL_BIN") or os.environ.get("SHELL") or "/bin/bash", "-l"]
    if agent["provider"] == "api":  # the built-in agent for OpenAI-compatible endpoints
        argv = [sys.executable, "-m", "switchboard.agent"]
        if agent.get("session_id"):
            argv += ["--resume" if has_history(agent) else "--session-id", agent["session_id"]]
        if agent.get("model"):
            argv += ["--model", agent["model"]]
        if agent.get("permission_mode") in ("auto", "bypassPermissions"):
            argv += ["--auto"]
        return argv + ["--append-system-prompt", shared.launch_note(agent)]
    if agent["provider"] == "hermes":  # the note travels in HERMES_EPHEMERAL_SYSTEM_PROMPT (launch_env)
        argv = [cli_bin("hermes"), "chat"] + (["--resume", agent["session_id"]] if agent.get("session_id") else [])
        if agent.get("model"):
            argv += ["-m", agent["model"]]
        if agent.get("effort"):
            argv += ["--reasoning", agent["effort"]]
        if agent.get("permission_mode") in ("auto", "bypassPermissions"):
            argv += ["--yolo"]
        return argv
    if agent["provider"] == "gemini":
        argv = [cli_bin("gemini")]
        if agent.get("model"):
            argv += ["--model", agent["model"]]
        if agent.get("permission_mode") in ("auto", "bypassPermissions"):
            argv += ["--yolo"]
        return argv
    exe = cli_bin(agent["provider"])
    src = fork_source(agent)
    note = shared.launch_note(agent)
    if agent["provider"] == "claude":
        argv = [exe]
        if agent.get("session_id"):
            if src and not has_history(agent):  # first launch of a summoned fork: copy the source conversation
                argv += ["--resume", src, "--fork-session", "--session-id", agent["session_id"]]
            else:
                argv += ["--resume" if has_history(agent) else "--session-id", agent["session_id"]]
        if agent.get("model"):
            argv += ["--model", agent["model"]]
        if agent.get("permission_mode"):
            argv += ["--permission-mode", agent["permission_mode"]]
        if agent.get("effort"):
            argv += ["--effort", agent["effort"]]
        return argv + ["--append-system-prompt", note]
    if agent.get("session_id"):
        argv = [exe, "resume", agent["session_id"]]
    else:
        argv = [exe] + (["fork", src] if src else [])  # the fork's own thread id is found once it starts
    if agent.get("model"):
        argv += ["--model", agent["model"]]
    return argv + ["-c", "developer_instructions=" + json.dumps(note)]


def fork_source(agent):
    """Session id a summoned side agent forks from (only its first launch uses it)."""
    try:
        return (json.loads(agent.get("fork_from") or "null") or {}).get("session")
    except (ValueError, AttributeError):
        return None


def launch_env(agent):
    env = discovery.account_env(agent["account"], agent["provider"]) if agent["provider"] != "shell" else dict(os.environ)
    for k in list(env):  # markers from a parent Claude session would turn off transcript saving
        if k.startswith(("TMUX", "SB_", "CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")) and \
                not k.startswith("SB_API_"):  # SB_API_*: the built-in agent's endpoint, from the account
            env.pop(k, None)
    env.update(SB_AGENT=agent["id"] if agent["provider"] != "shell" else "owner", TERM="xterm-256color",
               COLORTERM="truecolor", PATH=SB_BIN + os.pathsep + env.get("PATH", ""))
    if agent["provider"] == "api":  # the built-in agent writes its transcript under the hub's state dir
        env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["SB_STATE"] = STATE
    if agent["provider"] == "hermes":  # Hermes' --append-system-prompt: sent with every call, never stored
        env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = shared.launch_note(agent)
    return env


def processes_with(needle):
    """[(pid, cmdline)] of live processes whose command line mentions needle."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if needle in cmd and "switchboard.ptyholder" not in cmd:
            out.append((int(d), cmd.strip()))
    return out


# A running turn: nothing can be typed in until it ends.
TURN_MARKERS = [
    (re.compile(r"esc to interrupt", re.I), "a turn is running"),
    (re.compile(r"Waiting for \d+ background", re.I), "waiting on background agents"),
    (re.compile(r"\bWorking\b.*\(\d+s", re.I), "codex is working"),
    (re.compile(r"esc to cancel", re.I), "gemini is working"),
    (re.compile(r"enter interrupt\b|type to redirect this turn", re.I), "hermes is working"),
]
# Background work under an idle composer: a message may be typed in, but exiting the CLI would kill it.
BACKGROUND_MARKERS = [
    (re.compile(r"\b\d+\s+shells?\b", re.I), "background shells are running"),
    (re.compile(r"\b\d+\s+(background\s+)?(tasks?|monitors?)\b", re.I), "background tasks are running"),
    (re.compile(r"^\s*[◯◐●]\s+\S+.*\d+\s*[ms]\b.*tokens", re.M), "subagents are running"),
]
BUSY_MARKERS = TURN_MARKERS + BACKGROUND_MARKERS
PLACEHOLDERS = re.compile(r"^(Try \"|Ask Codex|Implement \{|Find and fix|Explain this|Summarize|Write tests|"
                          r"Type your message)")


def composer_text(screen):
    """Text sitting in the CLI's input box (Claude '❯ ' / Codex '› ' / Gemini '> ' prompt line), '' if empty."""
    lines = [l for l in screen.splitlines() if l.strip()]
    for line in reversed(lines[-12:]):
        m = re.match(r"^\s*[❯›>]\s?(.*)$", line)
        if m:
            text = m.group(1).strip()
            return "" if PLACEHOLDERS.match(text) else text  # a placeholder hint is not typed text
    return ""


class Term:
    def __init__(self, aid, state):
        self.aid = aid
        self.state = state
        self.provider = None
        self.sock = None
        self.lock = threading.RLock()
        self.screen = Screen(120, 40)
        self.buf = bytearray()
        self.listeners = set()
        self.last_output = 0.0
        self.alive = True
        self.exit_code = None
        self.size = (120, 40)

    def send(self, kind, payload):
        with self.lock:
            if not self.sock:
                raise ConnectionError("terminal not connected")
            self.sock.sendall(frame(kind, payload))

    def write(self, data):
        self.send(b"i", data.encode() if isinstance(data, str) else data)

    def resize(self, cols, rows):
        cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 300))
        with self.lock:
            self.size = (cols, rows)
            self.screen.resize(cols, rows)
        self.send(b"r", json.dumps({"cols": cols, "rows": rows}).encode())

    def signal(self, sig):
        self.send(b"k", str(int(sig)).encode())

    # ---- screen reading ----
    def text(self):
        with self.lock:
            return "\n".join(self.screen.lines(dim=False))

    def _composer(self):
        """(screen lines, index of the composer line or None). Gemini draws its composer in a box: "│ > text │"."""
        lines = [re.sub(r"^\s*[│┃]\s?(.*?)\s*[│┃]?\s*$", r"\1", l) for l in self.text().splitlines()]
        return lines, max((i for i, l in enumerate(lines) if re.match(r"^\s*[❯›]\s?", l) or re.match(r"^\s*>\s", l)),
                          default=None)

    def view(self):
        """(ready, busy, composer text) from the mirrored screen. busy = a turn is running; background shells and
        monitors under an idle composer do not count (a message can be typed in), see background()."""
        title = self.screen.title
        if title.startswith("sb:") and title != "sb:idle":  # the built-in agent says when it is working
            return True, True, ""
        lines, idx = self._composer()
        if idx is None:
            return False, False, ""
        tail = "\n".join(lines[max(0, idx - 4):])
        busy = any(rx.search(tail) for rx, _ in TURN_MARKERS)
        comp = composer_text("\n".join(lines[:idx + 1]))
        if comp and self.provider == "hermes":  # Hermes greys a suggestion in after the cursor of an empty composer
            y, x = self.screen.cursor
            if y == idx and x <= len(lines[idx]) - len(lines[idx].lstrip()) + 2:
                comp = ""
        return True, busy, comp

    def background(self):
        """True if the footer shows background shells, monitors or subagents, which exiting the CLI would end."""
        lines, idx = self._composer()
        tail = "\n".join(lines[max(0, idx - 4):] if idx is not None else lines[-12:])
        return any(rx.search(tail) for rx, _ in BACKGROUND_MARKERS)


class Terminals:
    def __init__(self, store):
        self.s = store
        self.terms = {}
        self.lock = threading.RLock()
        self.switching = {}  # aid -> {to, note}
        self.on_waiting = None  # hub callback(aid, waiting, why): a prompt only the owner can answer
        self.waiting = {}
        self.launched_at = {}
        self._procs_cache = (0.0, [])
        os.makedirs(PTY_DIR, mode=0o700, exist_ok=True)

    def paths(self, aid):
        b = os.path.join(PTY_DIR, aid)
        return {"sock": b + ".sock", "log": b + ".log", "state": b + ".json", "spec": b + ".spec.json"}

    # ---------- lifecycle ----------
    def reattach_all(self):
        """After a hub restart: reconnect to every holder that is still running."""
        for name in os.listdir(PTY_DIR):
            if name.endswith(".json") and not name.endswith(".spec.json"):
                aid = name[:-5]
                try:
                    st = json.load(open(os.path.join(PTY_DIR, name)))
                except (OSError, ValueError):
                    continue
                if "exit_code" not in st and discovery.pid_alive(st.get("holder_pid")):
                    try:
                        self._connect(aid)
                    except OSError:
                        pass

    def running(self, aid):
        t = self.terms.get(aid)
        return bool(t and t.alive)

    def term(self, aid):
        t = self.terms.get(aid)
        return t if t and t.alive else None

    def _connect(self, aid):
        p = self.paths(aid)
        with open(p["state"]) as f:
            st = json.load(f)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(p["sock"])
        t = Term(aid, st)
        t.sock = sock
        t.provider = (self.s.agent(aid) or {}).get("provider")
        # mirror at the CLI's real size: a relaunch keeps the old size and a reattached CLI has whatever the browser
        # set; a 120x40 mirror of a 227x71 screen wraps every line and hides both the footer and the composer
        old = self.terms.get(aid)
        size = cli_size(st.get("pid")) or (old.size if old else None)
        if size:
            t.size, t.screen = size, Screen(*size)
        with self.lock:
            old = self.terms.get(aid)
            if old:
                t.listeners = old.listeners
            self.terms[aid] = t
        threading.Thread(target=self._reader, args=(t,), daemon=True).start()
        return t

    def _reader(self, t):
        fr = FrameReader()
        try:
            while True:
                data = t.sock.recv(65536)
                if not data:
                    break
                for kind, payload in fr.feed(data):
                    if kind == b"o":
                        with t.lock:
                            t.buf += payload
                            if len(t.buf) > KEEP:
                                del t.buf[:len(t.buf) - KEEP]
                            t.screen.feed(payload)
                            t.last_output = time.time()
                            for fn in list(t.listeners):
                                fn("o", payload)
                    elif kind == b"x":
                        t.exit_code = int(payload or b"0")
        except OSError:
            pass
        with t.lock:
            t.alive = False
            for fn in list(t.listeners):
                fn("x", t.exit_code)
        self.s.event(t.aid, "system", f"CLI exited (code {t.exit_code})")
        self.s.set_agent(t.aid, state="idle", last_activity=now())

    def launch_block(self, agent):
        """Why this agent's CLI must not be started now, or None."""
        if agent.get("role") == "desk":
            return "the Switchboard chat runs headless; it has no terminal"
        if agent["provider"] not in PROVIDERS:
            return f"unknown provider {agent['provider']}"
        if self.running(agent["id"]):
            return None
        turn = self.s.one("SELECT turn_id FROM messages WHERE to_agent=? AND state='delivering' AND turn_id IS NOT NULL "
                          "AND turn_id NOT LIKE 'pty-%' LIMIT 1", (agent["id"],))
        if turn:
            return f"its headless turn {turn['turn_id']} is still running; it can open here when that turn ends"
        sid = agent.get("session_id")
        if sid:
            t0, procs = self._procs_cache
            if time.time() - t0 > 5:
                procs = processes_with("")
                self._procs_cache = (time.time(), procs)
            hits = [(p, c) for p, c in procs if sid in c and
                    (re.search(r"(^|/|\s)(claude|codex)(\s|$)", c) or "@openai/codex" in c) and
                    not (f"--session-id {sid}" not in c and ("--fork-session" in c or re.search(r"\sfork\s", c)))]
            if hits:
                pid, cmd = hits[0]
                where = ""
                for pane in discovery.tmux_panes() if agent.get("pane") or agent["mode"] == "observed" else []:
                    if pid in pane["pids"]:
                        where = f" in tmux {pane['target']}"
                return f"session is open in another process{where} (pid {pid}: {cmd[:60]}); exit it there first"
        if agent["provider"] == "codex" and not sid and agent["mode"] != "terminal":
            return "no Codex thread id known"
        if agent["provider"] != "shell" and not agent.get("account"):
            return "no account: pick one in the agent's controls (accounts are set up on the Setup page)"
        return None

    def launch(self, agent):
        aid = agent["id"]
        with self.lock:
            if self.running(aid):
                return self.terms[aid]
            self._procs_cache = (0.0, [])
            why = self.launch_block(agent)
            if why:
                raise PermissionError(why)
            if agent["mode"] != "terminal":
                self.s.set_agent(aid, mode="terminal", pane=None, gone=0)
                agent = self.s.agent(aid)
            if agent["provider"] in ("claude", "api") and not agent.get("session_id"):
                self.s.set_agent(aid, session_id=str(uuid.uuid4()))
                agent = self.s.agent(aid)
            p = self.paths(aid)
            old = self.terms.get(aid)
            cols, rows = old.size if old else (120, 40)
            argv = launch_argv(agent)
            spec = {"argv": argv, "cwd": agent.get("cwd") or os.path.expanduser("~"), "env": launch_env(agent),
                    "sock": p["sock"], "log": p["log"], "state": p["state"], "cols": cols, "rows": rows,
                    "agent": aid, "account": agent["account"], "session_id": agent.get("session_id")}
            fd = os.open(p["spec"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(spec, f)
            try:
                os.unlink(p["state"])
            except OSError:
                pass
            with open(p["log"] + ".err", "ab") as err:
                holder = subprocess.Popen([sys.executable, "-m", "switchboard.ptyholder", p["spec"]], cwd=ROOT,
                                          start_new_session=True, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=err, close_fds=True)
            threading.Thread(target=holder.wait, daemon=True).start()  # reap it if it exits while we run
            end = time.time() + 10
            while time.time() < end:
                if os.path.exists(p["state"]) and os.path.exists(p["sock"]):
                    try:
                        t = self._connect(aid)
                        break
                    except OSError:
                        pass
                time.sleep(0.05)
            else:
                raise RuntimeError("the terminal did not start (see " + p["log"] + ".err)")
            self.launched_at[aid] = time.time()
            self.s.set_agent(aid, state="idle", last_activity=now())
            self.s.event(aid, "system", "launched: " + " ".join(argv[:6]) + f" (account {agent['account']})")
            return t

    def exit_cli(self, aid, interrupt=False):
        """Leave the CLI cleanly: Esc + clear composer if asked, then /exit; SIGTERM/SIGKILL as a last resort."""
        t = self.term(aid)
        if not t:
            return True
        agent = self.s.agent(aid)
        if interrupt:
            t.write("\x1b")
            end = time.time() + 15
            while time.time() < end and t.view()[1]:
                time.sleep(0.3)
            if t.view()[2]:
                t.write("\x15")  # Ctrl+U: clear the composer line
                time.sleep(0.3)
                if t.view()[2]:
                    t.write("\x03")
                    time.sleep(0.3)
        if agent["provider"] == "shell":
            t.write("\x03")  # stop whatever is running, then leave the shell
            time.sleep(0.3)
            t.write("exit\r")
        else:
            t.write("/exit" if agent["provider"] in ("claude", "api", "hermes") else "/quit")
            time.sleep(0.4)
            t.write("\r")
        for sig, wait in ((None, EXIT_WAIT_S), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            if sig:
                try:
                    t.signal(sig)
                except OSError:
                    pass
            end = time.time() + wait
            while time.time() < end:
                if not t.alive:
                    return True
                time.sleep(0.2)
        return not t.alive

    def busy(self, aid):
        t = self.term(aid)
        if not t or (self.s.agent(aid) or {}).get("provider") == "shell":
            return False
        ready, busy, comp = t.view()
        return busy or bool(comp) or t.background()

    def stop(self, aid, confirm=False):
        if self.busy(aid) and not confirm:
            raise NeedsConfirm("the agent is working (or has unsent text or background jobs, which stopping ends); interrupt and stop it?")
        threading.Thread(target=self.exit_cli, args=(aid, True), daemon=True).start()

    def switch_account(self, aid, account, confirm=False):
        agent = self.s.agent(aid)
        if not self.running(aid):
            self.s.set_agent(aid, account=account)
            self.s.event(aid, "system", f"account set to {account}")
            return
        if self.busy(aid) and not confirm:
            raise NeedsConfirm("the agent is working (or has unsent text or background jobs, which a relaunch ends); interrupt it and switch accounts?")
        self.switching[aid] = {"to": account, "note": "exiting the CLI"}

        def run():
            try:
                if not self.exit_cli(aid, interrupt=True):
                    self.switching[aid]["note"] = "the CLI did not exit; switch abandoned"
                    time.sleep(10)
                    return
                self.s.set_agent(aid, account=account)
                self.s.event(aid, "system", f"account {agent['account']} -> {account}: relaunching the same session")
                self.switching[aid]["note"] = "relaunching"
                time.sleep(0.5)
                self.launch(self.s.agent(aid))
            except Exception as e:
                self.s.event(aid, "error", f"account switch failed: {e}")
            finally:
                self.switching.pop(aid, None)

        threading.Thread(target=run, daemon=True).start()

    def relaunch(self, aid, upd, confirm=False):
        """Exit the CLI and start it again on the same session with upd (model/effort) applied; a stopped
        terminal just starts with them."""
        what = ", ".join(f"{k} {v or 'default'}" for k, v in upd.items()) or "same settings"
        if not self.running(aid):
            self.s.set_agent(aid, **upd)
            self.s.event(aid, "system", f"relaunch ({what})")
            self.launch(self.s.agent(aid))
            return
        if self.busy(aid) and not confirm:
            raise NeedsConfirm("the agent is working (or has unsent text); interrupt it and relaunch?")
        self.switching[aid] = {"to": upd.get("model") or "relaunch", "note": "exiting the CLI"}

        def run():
            try:
                if not self.exit_cli(aid, interrupt=True):
                    self.switching[aid]["note"] = "the CLI did not exit; relaunch abandoned"
                    time.sleep(10)
                    return
                self.s.set_agent(aid, **upd)
                self.s.event(aid, "system", f"relaunching the same session ({what})")
                self.switching[aid]["note"] = "relaunching"
                time.sleep(0.5)
                self.launch(self.s.agent(aid))
            except Exception as e:
                self.s.event(aid, "error", f"relaunch failed: {e}")
            finally:
                self.switching.pop(aid, None)

        threading.Thread(target=run, daemon=True).start()

    def scrollback(self, aid):
        t = self.terms.get(aid)
        if t:
            with t.lock:
                return bytes(t.buf)
        try:
            with open(self.paths(aid)["log"], "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - KEEP))
                return f.read()
        except OSError:
            return b""

    # ---------- state for views ----------
    def status(self, agent):
        t = self.term(agent["id"])
        if not t:
            return {"running": False, "pid": None, "started_at": None, "state": "stopped"}
        ready, busy, _ = t.view()
        state = "busy" if busy else ("idle" if ready or agent["provider"] == "shell" else "starting")
        return {"running": True, "pid": t.state.get("pid"), "started_at": iso_from_epoch(t.state["started_at"]),
                "state": state}

    # ---------- delivery: sb send -> typed input ----------
    def tick(self):
        for t in list(self.terms.values()):
            if t.alive:
                ready, busy, _ = t.view()
                cur = self.s.agent(t.aid)
                if cur:
                    st = "busy" if busy else "idle"
                    upd = {"state": st}
                    if t.last_output and t.last_output > epoch(cur.get("last_activity")) + 1:
                        upd["last_activity"] = iso_from_epoch(t.last_output)
                    self.s.set_agent(t.aid, **upd)
                    if cur["provider"] == "codex" and not cur.get("session_id"):
                        self._find_codex_thread(cur, t)
                    elif cur["provider"] == "hermes" and not cur.get("session_id"):
                        self._find_hermes_session(cur, t)
                    self._check_waiting(t)
            elif self.waiting.pop(t.aid, None) and self.on_waiting:
                self.on_waiting(t.aid, False)
        self._confirm()
        rows = self.s.q("SELECT DISTINCT m.to_agent FROM messages m JOIN agents a ON a.id=m.to_agent "
                        "WHERE m.state='queued' AND a.mode='terminal'")
        for row in rows:
            agent = self.s.agent(row["to_agent"])
            if agent["paused"]:
                continue
            if self.s.one("SELECT 1 n FROM messages WHERE to_agent=? AND state='delivering'", (agent["id"],)):
                continue  # one typed message at a time; the next waits for its confirmation
            try:
                self._deliver_one(agent)
            except Exception as e:
                self.s.event(agent["id"], "error", f"delivery: {e}")

    WAITING = [(re.compile(r"trust (this folder|the files)|one you trust", re.I), "asks whether to trust its folder"),
               (re.compile(r"Do you want to (proceed|make this edit|create|run|allow|use)", re.I), "asks for permission"),
               (re.compile(r"Would you like to (run|make|apply)", re.I), "asks for approval"),
               (re.compile(r"Enter to (confirm|select)\s*·\s*Esc to (cancel|exit)", re.I), "has a choice open")]

    def _check_waiting(self, t):
        """Notice when a terminal sits on a prompt for the owner; only after it has stayed quiet for 5 s."""
        tail = "\n".join([l for l in t.text().splitlines() if l.strip()][-25:])
        why = next((w for rx, w in self.WAITING if rx.search(tail)), None)
        if why and time.time() - t.last_output < 5:
            return
        was = self.waiting.get(t.aid)
        if bool(why) != bool(was):
            self.waiting[t.aid] = why
            if not why:
                self.waiting.pop(t.aid, None)
            if self.on_waiting:
                self.on_waiting(t.aid, bool(why), why or "")

    def _deliver_one(self, agent):
        aid = agent["id"]
        t = self.term(aid)
        if not t:
            why = self.launch_block(agent)
            if why:
                self.s.x("UPDATE messages SET error=? WHERE to_agent=? AND state='queued'", ("waiting: " + why, aid))
                return
            self.launch(agent)
            return
        ready, busy, comp = t.view()
        if re.search(r"trust (this folder|the files)|one you trust", t.text(), re.I):
            self.s.x("UPDATE messages SET error=? WHERE to_agent=? AND state='queued'",
                     ("waiting: the CLI asks whether to trust its folder; answer it in the terminal", aid))
            return
        settled = time.time() - max(t.last_output, self.launched_at.get(aid, 0)) > 1.5
        if not ready or busy or comp or not settled or aid in self.switching:
            return
        with self.s.tx():
            m = self.s.one("SELECT * FROM messages WHERE to_agent=? AND state='queued' ORDER BY created_at LIMIT 1",
                           (aid,))
            if not m:
                return
            self.s.db.execute("UPDATE messages SET state='delivering', turn_id=?, attempts=attempts+1, "
                              "delivered_at=?, error=NULL WHERE id=? AND state='queued'", ("pty-" + m["id"], now(), m["id"]))
        text = m["body"]
        from .store import upload_path
        for u in json.loads(m.get("attachments") or "[]"):
            p, _ = upload_path(u)
            if p:
                text += " " + p
        if m["from_agent"] and m["from_agent"] != "owner":
            text = f"[from {m['from_agent']} via sb] {text}"
        t.write("\x1b[200~" + text + "\x1b[201~")
        time.sleep(0.5)
        t.write("\r")
        self.s.event(aid, "user", text, "pty-" + m["id"])
        time.sleep(1.5)
        # an Enter swallowed by a paste preview leaves our text in the composer: one more Enter submits it
        comp = t.view()[2]
        if comp and (comp[:20] in text or comp.startswith("[Pasted")):
            t.write("\r")

    def _confirm(self):
        for m in self.s.q("SELECT * FROM messages WHERE state IN ('delivering','delivered') AND turn_id LIKE 'pty-%'"):
            agent = self.s.agent(m["to_agent"])
            if not agent:
                continue
            path = discovery.transcript_path(agent["provider"], agent["session_id"])
            since = epoch(m["delivered_at"])
            if not discovery.transcript_format(agent["provider"]):
                self._confirm_screen(m, agent, since)
                continue
            if m["state"] == "delivering":
                body = m["body"]
                if m["from_agent"] and m["from_agent"] != "owner":
                    body = f"[from {m['from_agent']} via sb] {body}"
                if path and discovery.transcript_has_user_text(path, agent["provider"], body, since):
                    self.s.x("UPDATE messages SET state='delivered' WHERE id=?", (m["id"],))
                elif time.time() - since > CONFIRM_S:
                    self.s.x("UPDATE messages SET state='failed', finished_at=?, error=? WHERE id=?",
                             (now(), "typed once but not seen in the session transcript; not retyped", m["id"]))
                continue
            t = self.term(agent["id"])
            if t and t.view()[1]:
                continue
            if path:
                from . import transcript
                text, ts = transcript.last_reply(path, agent["provider"])
                if ts and epoch(ts) >= since - 1 and (not t or time.time() - t.last_output > 2):
                    self.s.x("UPDATE messages SET state='done', finished_at=?, reply=? WHERE id=?",
                             (now(), (text or "")[:20000], m["id"]))
            elif not t:
                self.s.x("UPDATE messages SET state='done', finished_at=? WHERE id=?", (now(), m["id"]))

    def _confirm_screen(self, m, agent, since):
        """CLIs whose sessions we cannot read (Gemini): delivered once the composer is empty again, done when the
        terminal has been idle and quiet for a few seconds; the reply is not recorded."""
        t = self.term(agent["id"])
        if not t:
            self.s.x("UPDATE messages SET state='done', finished_at=? WHERE id=?", (now(), m["id"]))
            return
        ready, busy, comp = t.view()
        quiet = time.time() - t.last_output > 4
        if m["state"] == "delivering" and ready and not comp and time.time() - since > 2:
            self.s.x("UPDATE messages SET state='delivered' WHERE id=?", (m["id"],))
        elif m["state"] == "delivered" and ready and not busy and quiet and time.time() - since > 6:
            self.s.x("UPDATE messages SET state='done', finished_at=? WHERE id=?", (now(), m["id"]))

    def _find_codex_thread(self, agent, t):
        started = t.state.get("started_at") or 0
        for mtime, path, meta in discovery.recent_codex_rollouts(1):
            if meta.get("cwd") == agent.get("cwd") and meta.get("id") and epoch(meta.get("timestamp")) >= started - 5:
                if not self.s.one("SELECT 1 n FROM agents WHERE session_id=?", (meta["id"],)):
                    self.s.set_agent(agent["id"], session_id=meta["id"])
                    return

    def _find_hermes_session(self, agent, t):
        from . import hermes
        taken = {r["session_id"] for r in self.s.q("SELECT session_id FROM agents WHERE session_id IS NOT NULL")}
        home = next((a["config_dir"] for a in discovery.accounts() if a["name"] == agent["account"]), None)
        sid = hermes.new_session(agent["cwd"], t.state.get("started_at") or 0, taken, home or None)
        if sid:
            self.s.set_agent(agent["id"], session_id=sid)


class NeedsConfirm(Exception):
    pass
