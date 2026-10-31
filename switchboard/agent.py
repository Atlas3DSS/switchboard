"""The built-in agent: a small terminal coding agent for any OpenAI-compatible chat endpoint (Anthropic, OpenAI,
Google, OpenRouter, Ollama, vLLM, LM Studio, ...). Switchboard runs it for `api` accounts the way it runs the Claude
Code and Codex CLIs: in a terminal, typed into once per message, read back from its transcript.

  python3 -m switchboard.agent [--session-id ID | --resume ID] [--model M] [--auto] [--append-system-prompt TEXT]
  python3 -m switchboard.agent -p [--tools sb] < prompt      # one headless turn, Claude-style stream-json out

The endpoint comes from SB_API_BASE / SB_API_KEY / SB_API_MODEL (set by the hub from your account). Tools: bash,
read_file, write_file, edit_file. Commands and edits ask first unless --auto (or /auto). The transcript is written
in Claude Code's jsonl shape to ~/.local/state/switchboard/api-sessions/<folder>/<session>.jsonl, so the hub's chat
view, search and delivery checks read it like any other session; each line also keeps the raw message for --resume.
"""
import argparse
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from . import config

STATE = os.path.expanduser(os.environ.get("SB_STATE", "~/.local/state/switchboard"))
SESSIONS = os.path.join(STATE, "api-sessions")
OUT_CAP = 30000
DIM, BOLD, CYAN, YELLOW, RED, RESET = "\x1b[2m", "\x1b[1m", "\x1b[36m", "\x1b[33m", "\x1b[31m", "\x1b[0m"

TOOLS = {
    "bash": {"description": "Run a shell command (bash) in the working folder and return its output. Use it to "
                            "inspect the project, run tests and builds, and use git.",
             "parameters": {"type": "object", "properties": {
                 "command": {"type": "string"},
                 "timeout_s": {"type": "integer", "description": "seconds before it is stopped (default 120)"}},
                 "required": ["command"]}},
    "read_file": {"description": "Read a text file. Lines come back numbered.",
                  "parameters": {"type": "object", "properties": {
                      "path": {"type": "string"}, "offset": {"type": "integer", "description": "first line (1-based)"},
                      "limit": {"type": "integer", "description": "number of lines (default 400)"}},
                      "required": ["path"]}},
    "write_file": {"description": "Create or overwrite a file with the given content.",
                   "parameters": {"type": "object", "properties": {
                       "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    "edit_file": {"description": "Replace an exact piece of text in a file. old_string must occur exactly once "
                                 "unless replace_all is true.",
                  "parameters": {"type": "object", "properties": {
                      "path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
                      "replace_all": {"type": "boolean"}}, "required": ["path", "old_string", "new_string"]}},
    "sb": {"description": "Run the Switchboard command line: `sb <args>` (for example: sessions \"words\", "
                          "resume <id>, send <agent> \"text\", summon <agent> \"task\" --fork).",
           "parameters": {"type": "object", "properties": {"args": {"type": "string"}}, "required": ["args"]}},
}
ASKS = {"bash": "Do you want to run this?", "write_file": "Do you want to create this file?",
        "edit_file": "Do you want to make this edit?"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def title(state):
    """busy / idle / ask in the window title: the hub reads it to know when it may type the next message."""
    sys.stdout.write(f"\x1b]0;sb:{state}\x07")
    sys.stdout.flush()


class Interrupted(Exception):
    pass


class Agent:
    def __init__(self, args):
        self.base = os.environ.get("SB_API_BASE") or ""
        self.key = os.environ.get("SB_API_KEY") or ""
        self.preset = os.environ.get("SB_API_PRESET") or "custom"
        self.model = args.model or os.environ.get("SB_API_MODEL") or ""
        self.auto = args.auto
        self.headless = args.print
        self.cwd = os.getcwd()
        self.tools = ["sb"] if args.tools == "sb" else ["bash", "read_file", "write_file", "edit_file"]
        self.sid = args.resume or args.session_id or str(uuid.uuid4())
        folder = re.sub(r"[^A-Za-z0-9]+", "-", self.cwd).strip("-")[:120] or "root"
        os.makedirs(os.path.join(SESSIONS, folder), mode=0o700, exist_ok=True)
        self.path = os.path.join(SESSIONS, folder, f"{self.sid}.jsonl")
        if args.resume:  # the session may have been started in another folder
            for d in os.listdir(SESSIONS):
                p = os.path.join(SESSIONS, d, f"{self.sid}.jsonl")
                if os.path.exists(p):
                    self.path = p
        system = (f"You are a capable software agent working in a terminal. Working folder: {self.cwd}. "
                  f"OS: {platform.system()} {platform.release()}. Today: {time.strftime('%Y-%m-%d')}. Use the tools to "
                  "look before you change anything, make focused changes, run tests when there are any, and finish "
                  "each turn with a short plain-text summary of what you did and what is left.")
        if args.append_system_prompt:
            system += "\n\n" + args.append_system_prompt
        self.messages = [{"role": "system", "content": system}]
        self.turns = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        if os.path.exists(self.path):
            self._load()

    # ---------- transcript ----------
    def _load(self):
        with open(self.path, errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e.get("oai"), dict):
                    self.messages.append(e["oai"])
        # an assistant tool call cut off before its results would make the endpoint refuse the history
        while self.messages and self.messages[-1].get("role") == "assistant" and self.messages[-1].get("tool_calls"):
            self.messages.pop()

    def _write(self, entry):
        entry.update(sessionId=self.sid, cwd=self.cwd, timestamp=now(), uuid=str(uuid.uuid4()),
                     entrypoint="sdk-cli" if self.headless else "cli")
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")  # compact, like Claude Code (readers grep it)

    def record_user(self, text):
        msg = {"role": "user", "content": text}
        self.messages.append(msg)
        self._write({"type": "user", "message": {"role": "user", "content": text}, "oai": msg})

    def record_assistant(self, msg, usage):
        content = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        for c in msg.get("tool_calls") or []:
            try:
                inp = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                inp = {"raw": c["function"].get("arguments")}
            content.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": inp})
        self.messages.append(msg)
        self._write({"type": "assistant", "oai": msg,
                     "message": {"id": "msg_" + uuid.uuid4().hex[:20], "role": "assistant", "model": self.model,
                                 "content": content,
                                 "usage": {"input_tokens": usage.get("prompt_tokens") or 0,
                                           "output_tokens": usage.get("completion_tokens") or 0}}})

    def record_tool(self, call_id, text):
        msg = {"role": "tool", "tool_call_id": call_id, "content": text}
        self.messages.append(msg)
        self._write({"type": "user", "oai": msg, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": text[:4000]}]}})

    # ---------- the endpoint ----------
    def complete(self, on_text):
        """One streamed chat completion. Returns (assistant message, usage)."""
        body = {"model": self.model, "messages": self.messages, "stream": True,
                "tools": [{"type": "function", "function": {"name": n, **TOOLS[n]}} for n in self.tools],
                "stream_options": {"include_usage": True}}
        if self.preset == "anthropic" or "anthropic.com" in self.base:
            body["max_tokens"] = 32000
        if not self.base or not self.model:
            raise RuntimeError("no endpoint or model: set up an API account on the Switchboard Setup page")
        for attempt in range(4):
            req = urllib.request.Request(config.url(self.base, "chat/completions"), data=json.dumps(body).encode(),
                                         headers=config.headers(self.base, self.key, self.preset))
            try:
                resp = urllib.request.urlopen(req, timeout=600)
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:1500]
                if e.code == 400 and "stream_options" in body and "stream_options" in detail:
                    body.pop("stream_options")
                    continue
                if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                    time.sleep(2 * 2 ** attempt)
                    continue
                raise RuntimeError(f"HTTP {e.code} from {self.base}: {detail}")
            except urllib.error.URLError as e:
                raise RuntimeError(f"cannot reach {self.base}: {e.reason}")
        text, calls, usage = [], {}, {}
        with resp:
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise RuntimeError(f"endpoint error: {json.dumps(chunk['error'])[:800]}")
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    d = choice.get("delta") or {}
                    if d.get("content"):
                        text.append(d["content"])
                        on_text(d["content"])
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["args"] += fn.get("arguments") or ""
        msg = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            msg["tool_calls"] = [{"id": c["id"] or "call_" + uuid.uuid4().hex[:12], "type": "function",
                                  "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                 for _, c in sorted(calls.items())]
        return msg, usage

    # ---------- tools ----------
    def run_tool(self, name, args):
        if name not in self.tools:
            return f"error: no tool named {name}"
        try:
            if name == "bash":
                return self._bash(args["command"], int(args.get("timeout_s") or 120))
            if name == "sb":
                return self._run(["sb"] + shlex.split(args.get("args") or ""), 120)
            path = os.path.join(self.cwd, os.path.expanduser(args["path"]))
            if name == "read_file":
                with open(path, errors="replace") as f:
                    lines = f.readlines()
                start = max(1, int(args.get("offset") or 1))
                n = int(args.get("limit") or 400)
                out = "".join(f"{i:6}\t{l}" for i, l in enumerate(lines[start - 1:start - 1 + n], start))
                more = len(lines) - (start - 1 + n)
                return (out or "(empty file)")[:OUT_CAP] + (f"\n... {more} more lines" if more > 0 else "")
            if name == "write_file":
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w") as f:
                    f.write(args["content"])
                return f"wrote {len(args['content'])} characters to {path}"
            if name == "edit_file":
                with open(path) as f:
                    s = f.read()
                n = s.count(args["old_string"])
                if n == 0:
                    return "error: old_string not found; read the file and copy the text exactly"
                if n > 1 and not args.get("replace_all"):
                    return f"error: old_string occurs {n} times; add context or set replace_all"
                with open(path, "w") as f:
                    f.write(s.replace(args["old_string"], args["new_string"], -1 if args.get("replace_all") else 1))
                return f"edited {path} ({n if args.get('replace_all') else 1} replacement)"
        except KeyError as e:
            return f"error: missing argument {e}"
        except (OSError, ValueError) as e:
            return f"error: {e}"
        return "error: unknown tool"

    def _bash(self, command, timeout):
        return self._run(["bash", "-c", command], timeout)

    def _run(self, argv, timeout):
        try:
            p = subprocess.run(argv, cwd=self.cwd, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, errors="replace")
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            return out[-OUT_CAP:] + f"\n[stopped after {timeout} s]"
        except OSError as e:
            return f"error: {e}"
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        if len(out) > OUT_CAP:
            out = out[:OUT_CAP // 3] + f"\n... [{len(out) - OUT_CAP} characters cut] ...\n" + out[-2 * OUT_CAP // 3:]
        return (out.strip() or "(no output)") + f"\n[exit {p.returncode}]"

    def approve(self, name, args):
        if self.auto or self.headless or name in ("read_file", "sb"):
            return True
        if name == "bash":
            print(f"{YELLOW}$ {args.get('command', '')}{RESET}")
        elif name == "write_file":
            body = args.get("content", "")
            print(f"{YELLOW}write {args.get('path')}{RESET} ({len(body.splitlines())} lines)")
            print(DIM + "\n".join(body.splitlines()[:20]) + RESET)
        elif name == "edit_file":
            print(f"{YELLOW}edit {args.get('path')}{RESET}")
            for l in (args.get("old_string") or "").splitlines()[:12]:
                print(f"{RED}- {l}{RESET}")
            for l in (args.get("new_string") or "").splitlines()[:12]:
                print(f"\x1b[32m+ {l}{RESET}")
        title("ask")
        try:
            ans = input(f"{ASKS[name]} [y]es / [a]lways / [n]o: ").strip().lower()
        except EOFError:
            ans = "n"
        finally:
            title("busy")
        if ans in ("a", "always"):
            self.auto = True
            return True
        return ans in ("y", "yes")

    # ---------- one turn ----------
    def turn(self, text, emit=None):
        """Send text, then call the model and run its tools until it answers without tools. Returns the final text."""
        self.record_user(text)
        final, steps, t0 = "", 0, time.time()
        while True:
            steps += 1
            if not self.headless:
                sys.stdout.write("\n")
            msg, usage = self.complete(lambda s: None if self.headless else (sys.stdout.write(s), sys.stdout.flush()))
            for k in ("prompt_tokens", "completion_tokens"):
                self.usage["input_tokens" if k == "prompt_tokens" else "output_tokens"] += usage.get(k) or 0
            self.record_assistant(msg, usage)
            if msg.get("content"):
                final = msg["content"]
            if emit:
                emit({"type": "assistant", "message": {"role": "assistant", "model": self.model,
                                                       "content": ([{"type": "text", "text": msg["content"]}]
                                                                   if msg.get("content") else []) +
                                                       [{"type": "tool_use", "name": c["function"]["name"],
                                                         "input": _args(c)} for c in msg.get("tool_calls") or []]}})
            if not msg.get("tool_calls"):
                if not self.headless:
                    sys.stdout.write("\n")
                return final, steps, time.time() - t0
            for c in msg["tool_calls"]:
                name, args = c["function"]["name"], _args(c)
                if not self.headless:
                    brief = args.get("command") or args.get("path") or args.get("args") or ""
                    print(f"\n{CYAN}● {name}{RESET} {DIM}{str(brief)[:200]}{RESET}")
                if self.approve(name, args):
                    out = self.run_tool(name, args)
                else:
                    out = "the user declined this; ask them or try another way"
                if not self.headless:
                    lines = out.splitlines()
                    print(DIM + "\n".join("  " + l for l in lines[:8]) + (f"\n  … {len(lines) - 8} more lines"
                                                                         if len(lines) > 8 else "") + RESET)
                self.record_tool(c["id"], out)

    # ---------- modes ----------
    def interactive(self):
        try:
            import readline  # noqa: F401  (line editing and history for input())
        except ImportError:
            pass
        host = re.sub(r"^https?://", "", self.base).split("/")[0]
        home = os.path.expanduser("~")
        where = "~" + self.cwd[len(home):] if self.cwd == home or self.cwd.startswith(home + os.sep) else self.cwd
        print(f"{BOLD}Switchboard agent{RESET} {DIM}· {self.model or 'no model set'} · {host} · {where}{RESET}")
        print(f"{DIM}/model <name> · /auto (stop asking before commands) · /clear · /exit · Ctrl+C stops a turn{RESET}")
        if len(self.messages) > 1:
            print(f"{DIM}resumed session {self.sid} ({len(self.messages) - 1} messages){RESET}")
        if not self.base or not self.model:
            print(f"{RED}No endpoint or model: add an API account on the Switchboard setup page.{RESET}")
        while True:
            title("idle")
            try:
                print()
                text = input("❯ ").strip()
            except EOFError:
                return
            except KeyboardInterrupt:
                print()
                continue
            if not text:
                continue
            if text in ("/exit", "/quit"):
                return
            if text.startswith("/model"):
                self.model = text[6:].strip() or self.model
                print(f"{DIM}model: {self.model}{RESET}")
                continue
            if text == "/auto":
                self.auto = not self.auto
                print(f"{DIM}auto-approve is {'on' if self.auto else 'off'}{RESET}")
                continue
            if text == "/clear":
                self.sid = str(uuid.uuid4())
                self.path = os.path.join(os.path.dirname(self.path), f"{self.sid}.jsonl")
                self.messages = self.messages[:1]
                print(f"{DIM}new session {self.sid}{RESET}")
                continue
            title("busy")
            try:
                self.turn(text)
            except KeyboardInterrupt:
                print(f"\n{DIM}(interrupted){RESET}")
                while self.messages[-1].get("role") == "assistant" and self.messages[-1].get("tool_calls"):
                    self.messages.pop()
            except Exception as e:  # a bad endpoint or reply must not end the session
                print(f"\n{RED}{e}{RESET}")

    def headless_turn(self, text):
        def emit(e):
            sys.stdout.write(json.dumps(e) + "\n")
            sys.stdout.flush()
        emit({"type": "system", "subtype": "init", "session_id": self.sid, "model": self.model, "cwd": self.cwd})
        try:
            final, steps, secs = self.turn(text, emit)
            ok = True
        except Exception as e:  # reported as the turn's result
            final, steps, secs, ok = str(e), 0, 0, False
        emit({"type": "result", "subtype": "success" if ok else "error", "is_error": not ok, "result": final,
              "session_id": self.sid, "num_turns": steps, "duration_ms": int(secs * 1000), "usage": self.usage})


def _args(call):
    try:
        v = json.loads(call["function"].get("arguments") or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def main():
    ap = argparse.ArgumentParser(prog="switchboard.agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session-id")
    ap.add_argument("--resume")
    ap.add_argument("--model")
    ap.add_argument("--auto", action="store_true", help="run commands and edits without asking")
    ap.add_argument("--append-system-prompt")
    ap.add_argument("-p", "--print", action="store_true", help="one headless turn: prompt on stdin, stream-json out")
    ap.add_argument("--tools", choices=["all", "sb"], default="all")
    args = ap.parse_args()
    agent = Agent(args)
    if args.print:
        agent.headless_turn(sys.stdin.read())
    else:
        agent.interactive()


if __name__ == "__main__":
    main()
