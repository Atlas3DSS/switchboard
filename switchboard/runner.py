"""Turn delivery.

managed agents: one headless CLI process per turn, detached (survives a hub restart), stdout to
turns/<turn>.jsonl, which the hub tails. observed agents (tmux panes): a one-line pointer is typed into
the pane only while the session is idle, and delivery is confirmed from the session transcript, never
from the screen. Nothing is ever re-typed: a pointer that does not land is marked held.
"""
import base64
import json
import os
import shlex
import subprocess
import sys
import time
import uuid

from . import discovery

from .store import INBOX, TURNS, epoch, new_id, now, upload_path

ACCOUNT_TURN_CAP = 4
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SB_BIN = os.path.join(ROOT, "bin")


def compose_prompt(msgs):
    if len(msgs) == 1 and msgs[0]["from_agent"] == "owner":
        return msgs[0]["body"]
    parts = []
    for m in msgs:
        who = "the owner" if m["from_agent"] == "owner" else f"agent {m['from_agent']}"
        tag = " (reply to your earlier message; no reply needed)" if m["is_reply"] else ""
        parts.append(f"[switchboard message {m['id']} from {who}{tag}]\n{m['body']}")
    footer = ("\n\n(Your final answer to this turn is delivered back to each sender automatically; do not "
              "`sb send` a reply. To start a new conversation with another agent: `sb send <agent> \"text\"`; "
              "to ask the owner a question: `sb ask \"question\" --options a,b`; `sb agents` lists agents.)")
    return "\n\n".join(parts) + (footer if any(m["from_agent"] != "owner" for m in msgs) else "")


def switchboard_note(agent):
    return (f"You are agent '{agent['id']}' on the owner's Switchboard, project "
            f"'{agent.get('project') or 'unsorted'}'. The owner reads and answers you there, not in a terminal, so "
            "end every turn with a clear, self-contained reply. Message other agents only with "
            "`sb send <agent> \"text\"` (add --wait to get the reply), list them with `sb agents`, and ask the owner "
            "a decision with `sb ask \"question\" --options a,b`. "
            "Background tasks end when your turn ends, so start long-running work as a separate service or "
            "detached process and check on it in later turns.")


ROUTER_MAX_CONTEXT = 40_000
ROUTER_NOTE = (
    "You are Switchboard, the owner's chat window over all their projects and agents. Your one job: get them to "
    "the right project agent fast. "
    "Answer in one to three short sentences, no preamble. The live project list below is current; trust it over "
    "anything you remember. When they want a project or an agent, end your answer with [[open:<project-id>/<agent-id>]] "
    "(or [[open:<project-id>]] for the whole project); the page turns that into a button that opens its terminal panes. "
    "Do not do project work yourself and do not run tools, except `sb sessions`/`sb resume` (below) and when they "
    "explicitly asks you to pass a message on: then run `sb send <agent> \"...\"` once and say so. If nothing fits, "
    "name the closest project or suggest \"+ Agent\" on its card.\n\n"
    "PAST SESSIONS: when the owner wants an old conversation (\"the session where we…\", \"resume the parser one\"), run "
    "`sb sessions \"<a few distinctive words>\"` (newest-relevant first; each row says which agent it already is or "
    "which project it would join). If one clearly fits, run `sb resume <session-id>` once (it reuses an existing "
    "agent, or adds one, making a project when none fits) and reply with the [[open:…]] it prints. If several fit, "
    "list up to three (date, title, folder) and ask which.\n\n"
    "SIDE AGENTS: when the owner wants a side question or task handled beside a working agent without disturbing it "
    "(\"btw, ask a copy of the api agent…\", \"summon a helper for the web agent\"), run `sb summon <agent> \"<task>\" --fork` "
    "once (drop --fork for a fresh agent that pulls context only when needed) and reply with the [[open:…]] it "
    "prints.")


def turn_command(agent, prompt_file, first_turn, images=()):
    """argv for one headless turn. The prompt is fed on stdin."""
    if agent["provider"] == "claude":
        argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--permission-mode", agent.get("permission_mode") or "auto"]
        argv += (["--session-id", agent["session_id"]] if first_turn else ["--resume", agent["session_id"]])
        note = switchboard_note(agent)
        if agent.get("role") == "desk":
            # The router must answer in seconds: no MCP servers, a short note and a small live project summary
            # (in the system prompt, so it is not piled up in the transcript turn after turn).
            argv += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                     "--tools=Bash", "--allowedTools=Bash(sb *)"]  # it may only run `sb`; no file searching
            note = ROUTER_NOTE + "\n\n" + (agent.get("_summary") or "")
        argv += ["--append-system-prompt", note]
        if agent.get("model"):
            argv += ["--model", agent["model"]]
        if agent.get("effort"):
            argv += ["--effort", agent["effort"]]
        return argv
    if agent["provider"] == "api":  # the built-in agent, one headless turn (prompt on stdin)
        argv = [sys.executable, "-m", "switchboard.agent", "-p",
                "--session-id" if first_turn else "--resume", agent["session_id"]]
        note = switchboard_note(agent)
        if agent.get("role") == "desk":
            argv += ["--tools", "sb"]  # it may only run `sb`
            note = ROUTER_NOTE + "\n\n" + (agent.get("_summary") or "")
        if agent.get("model"):
            argv += ["--model", agent["model"]]
        return argv + ["--append-system-prompt", note]
    if agent["provider"] == "codex":
        base = ["codex", "exec"]
        flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if agent.get("model"):
            flags += ["-m", agent["model"]]
        if agent.get("effort"):
            flags += ["-c", f"model_reasoning_effort={agent['effort']}"]
        for p, _ in images:
            flags += ["-i", p]
        if first_turn or not agent.get("session_id"):
            return base + flags + ["-C", agent["cwd"], "-"]
        return base + ["resume"] + flags + [agent["session_id"], "-"]
    raise ValueError(f"provider {agent['provider']} cannot run turns yet")


def attachment_paths(msgs):
    """[(path, media_type)] for every image attached to these messages, in order."""
    out = []
    for m in msgs:
        for uid in json.loads(m.get("attachments") or "[]"):
            p, mt = upload_path(uid)
            if p:
                out.append((p, mt))
    return out


def claude_prompt_line(prompt, images=()):
    if not images:
        content = prompt
    else:
        content = ([{"type": "text", "text": prompt}] if prompt else []) + [
            {"type": "image", "source": {"type": "base64", "media_type": mt,
                                         "data": base64.b64encode(open(p, "rb").read()).decode()}}
            for p, mt in images]
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def with_image_paths(prompt, images):
    """Text form of attachments for transports that cannot carry image blocks (tmux panes)."""
    if not images:
        return prompt
    listing = "\n".join(f"- {p}" for p, _ in images)
    return f"{prompt}\n\n[Attached image(s) — open them with your Read tool:]\n{listing}".strip()


class Runner:
    def __init__(self, store):
        self.s = store
        self.summary_fn = None  # set by the hub: live one-line-per-project summary for the router

    # ---------- scheduling ----------
    def running_turns(self):
        return self.s.q("SELECT DISTINCT turn_id, to_agent, pid FROM messages WHERE state='delivering' "
                        "AND turn_id IS NOT NULL AND turn_id NOT LIKE 'pty-%'")

    def can_start(self, agent):
        running = self.running_turns()
        if any(r["to_agent"] == agent["id"] for r in running):
            return False, "busy"
        agents = {a["id"]: a for a in self.s.agents(include_gone=True)}
        same_account = [r for r in running if agents.get(r["to_agent"], {}).get("account") == agent["account"]
                        and agents.get(r["to_agent"], {}).get("mode") == "managed"]
        if agent["mode"] == "managed" and len(same_account) >= ACCOUNT_TURN_CAP:
            return False, f"account {agent['account']} at {ACCOUNT_TURN_CAP} concurrent turns"
        return True, ""

    def tick(self):
        """Advance running turns, then start new ones for agents with queued mail."""
        for r in self.running_turns():
            if r["turn_id"].startswith("p-"):  # pane deliveries are confirmed by poll_panes
                continue
            try:
                self.poll_turn(r["turn_id"])
            except Exception as e:  # never let one bad turn stop the loop
                self.s.event(r["to_agent"], "error", f"poll failed: {e}", r["turn_id"])
        queued = self.s.q("SELECT to_agent, COUNT(*) n FROM messages WHERE state='queued' GROUP BY to_agent")
        for row in queued:
            agent = self.s.agent(row["to_agent"])
            if not agent:
                self._fail_queued(row["to_agent"], "unknown agent")
                continue
            if agent["paused"] or agent["attached"] or agent["mode"] == "terminal":
                continue
            proj = self.s.project(agent["project"]) if agent.get("project") else None
            if proj and proj.get("paused"):
                continue
            ok, why = self.can_start(agent)
            if not ok:
                continue
            try:
                if agent["mode"] == "managed":
                    self.start_turn(agent)
                else:
                    self.pane_deliver(agent)
            except Exception as e:
                self.s.event(agent["id"], "error", f"delivery failed: {e}")
                self._fail_queued(agent["id"], str(e))

    def _fail_queued(self, agent_id, err):
        self.s.x("UPDATE messages SET state='failed', error=?, finished_at=? WHERE to_agent=? AND state='queued'",
                 (err, now(), agent_id))

    # ---------- managed turns ----------
    def start_turn(self, agent):
        turn = new_id("t")
        with self.s.tx():
            msgs = self.s.q("SELECT * FROM messages WHERE to_agent=? AND state='queued' ORDER BY created_at LIMIT 10",
                            (agent["id"],))
            if not msgs:
                return
            ids = [m["id"] for m in msgs]
            self.s.db.execute(f"UPDATE messages SET state='delivering', turn_id=?, attempts=attempts+1, "
                              f"delivered_at=? WHERE id IN ({','.join('?' * len(ids))})", (turn, now(), *ids))
        if agent.get("role") == "desk":
            agent["_summary"] = self.summary_fn() if self.summary_fn else ""
            if (agent.get("context_tokens") or 0) > ROUTER_MAX_CONTEXT:  # it routes; it needs no long memory
                agent["session_id"] = None
                self.s.set_agent(agent["id"], session_id=None, context_tokens=0)
        first_turn = not agent.get("session_id") or not self._has_history(agent)
        if agent["provider"] in ("claude", "api") and not agent.get("session_id"):
            agent["session_id"] = str(uuid.uuid4())
            self.s.set_agent(agent["id"], session_id=agent["session_id"])
        prompt = compose_prompt(msgs)
        images = attachment_paths(msgs)
        if agent["provider"] == "codex":
            prompt = f"[{switchboard_note(agent)}]\n\n{prompt}"
            first_input = prompt
        elif agent["provider"] == "api":
            first_input = with_image_paths(prompt, images)
        else:
            first_input = claude_prompt_line(prompt, images)
        prompt_file = os.path.join(TURNS, f"{turn}.prompt")
        with open(prompt_file, "w") as f:
            f.write(prompt + ("\n" + "\n".join(p for p, _ in images) if images else ""))
        argv = turn_command(agent, prompt_file, first_turn, images if agent["provider"] == "codex" else ())
        env = discovery.account_env(agent["account"], agent["provider"])
        env["SB_AGENT"] = agent["id"]
        env["SB_TURN"] = turn
        env["SB_HOP"] = str(max(m["hop"] for m in msgs))
        # A background job finishing after a headless turn forks the transcript, and the next resume then
        # follows the wrong branch. Long work belongs in systemd units, so managed turns run without them.
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        env["PATH"] = SB_BIN + os.pathsep + env.get("PATH", "")
        env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        out = open(os.path.join(TURNS, f"{turn}.jsonl"), "w")
        err = open(os.path.join(TURNS, f"{turn}.stderr"), "w")
        proc = subprocess.Popen(argv, cwd=agent["cwd"] or os.path.expanduser("~"), stdin=subprocess.PIPE,
                                stdout=out, stderr=err, env=env, start_new_session=True)
        out.close()
        err.close()
        proc.stdin.write(first_input.encode())
        proc.stdin.flush()
        self._stdin = getattr(self, "_stdin", {})
        if agent["provider"] == "claude":
            # Claude stream-json keeps reading user messages: the owner can inject into this turn until
            # its first result, when the hub closes the channel (lines already written are still read).
            self._stdin[turn] = proc.stdin
        else:
            proc.stdin.close()
        self.s.x("UPDATE messages SET pid=? WHERE turn_id=?", (proc.pid, turn))
        self.s.set_agent(agent["id"], state="busy", last_activity=now())
        self.s.event(agent["id"], "user", prompt + (f"\n[{len(images)} image(s) attached]" if images else ""), turn)
        self.s.event(agent["id"], "system", f"turn {turn} started: {' '.join(shlex.quote(a) for a in argv[:6])} …",
                     turn)
        self._procs = getattr(self, "_procs", {})
        self._procs[turn] = proc

    def injectable_turn(self, agent_id):
        """The running turn of this agent that still accepts injected messages, or None."""
        for t, pipe in list(getattr(self, "_stdin", {}).items()):
            if pipe.closed:
                continue
            row = self.s.one("SELECT to_agent FROM messages WHERE turn_id=? AND state='delivering' LIMIT 1", (t,))
            if row and row["to_agent"] == agent_id:
                return t
        return None

    def inject(self, message):
        """Write a queued message into the agent's running turn. Returns True if it went in."""
        turn = self.injectable_turn(message["to_agent"])
        if not turn:
            return False
        m = dict(message)
        prompt = compose_prompt([m])
        if m["from_agent"] == "owner":
            prompt = "[The owner sent this while you were working — read it now and adjust.]\n" + prompt
        line = claude_prompt_line(prompt, attachment_paths([m]))
        pipe = self._stdin[turn]
        try:
            pipe.write(line.encode())
            pipe.flush()
        except (BrokenPipeError, ValueError, OSError):
            self._stdin.pop(turn, None)
            return False
        pid = self.s.one("SELECT pid FROM messages WHERE turn_id=? LIMIT 1", (turn,))["pid"]
        self.s.x("UPDATE messages SET state='delivering', turn_id=?, pid=?, attempts=attempts+1, delivered_at=?, "
                 "mode='inject' WHERE id=? AND state='queued'", (turn, pid, now(), m["id"]))
        self.s.event(m["to_agent"], "user", "(injected) " + prompt, turn)
        return True

    def interrupt(self, agent_id, note="stopped by the owner"):
        """Stop the running turn now. Its messages end as 'interrupted'; queued mail starts the next turn."""
        rows = self.s.q("SELECT DISTINCT turn_id, pid FROM messages WHERE to_agent=? AND state IN "
                        "('delivering','delivered')", (agent_id,))
        agent = self.s.agent(agent_id)
        for r in rows:
            if r["turn_id"] and r["turn_id"].startswith("p-"):
                if agent and agent.get("pane"):
                    subprocess.run(["tmux", "send-keys", "-t", agent["pane"], "Escape"], timeout=5)
            else:
                pipe = getattr(self, "_stdin", {}).pop(r["turn_id"], None)
                if pipe:
                    try:
                        pipe.close()
                    except OSError:
                        pass
                self._kill(r["pid"])
                getattr(self, "_procs", {}).pop(r["turn_id"], None)
                getattr(self, "_results", {}).pop(r["turn_id"], None)
            self.s.x("UPDATE messages SET state='interrupted', error=?, finished_at=? "
                     "WHERE turn_id=? AND state IN ('delivering','delivered')", (note, now(), r["turn_id"]))
            self.s.event(agent_id, "system", f"turn {r['turn_id']} {note}", r["turn_id"])
        if agent:
            self.s.set_agent(agent_id, state="idle", last_activity=now())
        return len(rows)

    def _has_history(self, agent):
        if agent["provider"] in ("claude", "api"):
            return bool(discovery.transcript_path(agent["provider"], agent["session_id"]))
        return bool(agent.get("session_id"))

    def poll_turn(self, turn):
        msgs = self.s.q("SELECT * FROM messages WHERE turn_id=?", (turn,))
        if not msgs:
            return
        agent = self.s.agent(msgs[0]["to_agent"])
        pid = msgs[0]["pid"]
        path = os.path.join(TURNS, f"{turn}.jsonl")
        offset_key = f"_off_{turn}"
        offsets = getattr(self, "_offsets", {})
        self._offsets = offsets
        off = offsets.get(offset_key, None)
        if off is None:  # after a hub restart, events already recorded for this turn are skipped by count
            off = 0
            seen = self.s.one("SELECT COUNT(*) n FROM events WHERE turn_id=? AND kind NOT IN ('user','system')",
                              (turn,))["n"]
            offsets[f"_skip_{turn}"] = seen
        results = getattr(self, "_results", {})
        self._results = results
        if os.path.exists(path):
            with open(path, errors="replace") as f:
                f.seek(off)
                chunk = f.read()
                complete = chunk[:chunk.rfind("\n") + 1] if "\n" in chunk else ""
                offsets[offset_key] = off + len(complete.encode())
            for line in complete.splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                res = self._record(agent, e, turn)
                if res is not None:  # a resumed session can emit several results; the last one counts
                    results[turn] = dict(res, at=time.time())
                    pipe = getattr(self, "_stdin", {}).pop(turn, None)
                    if pipe:  # no more injection; lines already written are still processed before exit
                        try:
                            pipe.close()
                        except OSError:
                            pass
        proc = getattr(self, "_procs", {}).get(turn)
        alive = proc.poll() is None if proc is not None else discovery.pid_alive(pid)
        result = results.get(turn)
        if alive:
            if result and time.time() - result["at"] > 120:
                self._kill(pid)  # result written but the CLI never exited
            elif epoch(msgs[0]["delivered_at"]) < time.time() - 6 * 3600:
                self._kill(pid)
                results[turn] = result = {"ok": False, "text": "turn exceeded 6 h and was stopped"}
            else:
                return
        if result is None:
            stderr = ""
            try:
                stderr = open(os.path.join(TURNS, f"{turn}.stderr"), errors="replace").read()[-1500:]
            except OSError:
                pass
            result = {"ok": False, "text": f"turn process exited without a result. {stderr}".strip()}
        results.pop(turn, None)
        self._finish(agent, msgs, turn, result)

    def _record(self, agent, e, turn):
        """Turn one stream event into hub events; returns a result dict at the end of the turn."""
        skip_key = f"_skip_{turn}"
        offsets = self._offsets

        def ev(kind, text):
            if offsets.get(skip_key, 0) > 0:
                offsets[skip_key] -= 1
                return
            self.s.event(agent["id"], kind, text, turn)

        t = e.get("type")
        if agent["provider"] != "codex":  # Claude Code and the built-in agent speak the same stream-json
            if t == "system" and e.get("subtype") == "init":
                if e.get("session_id") and e["session_id"] != agent.get("session_id"):
                    self.s.set_agent(agent["id"], session_id=e["session_id"])
            elif t == "assistant":
                for c in (e.get("message") or {}).get("content") or []:
                    if c.get("type") == "text" and c.get("text", "").strip():
                        ev("assistant", c["text"])
                        self.s.set_agent(agent["id"], last_text=c["text"][-400:], last_activity=now())
                    elif c.get("type") == "tool_use":
                        inp = c.get("input") or {}
                        brief = inp.get("description") or inp.get("command") or inp.get("file_path") or \
                            inp.get("prompt") or json.dumps(inp)
                        ev("tool", f"{c.get('name')}: {str(brief)[:300]}")
            elif t == "result":
                text = e.get("result") or ""
                ok = not e.get("is_error") and e.get("subtype") == "success"
                ev("result", f"{'done' if ok else 'error'} · {e.get('num_turns', '?')} steps · "
                             f"{round((e.get('duration_ms') or 0) / 1000)} s")
                if e.get("session_id"):
                    self.s.set_agent(agent["id"], session_id=e["session_id"])
                u = e.get("usage") or {}
                self.s.set_agent(agent["id"], last_turn=json.dumps({
                    "input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) +
                    (u.get("cache_read_input_tokens") or 0), "output_tokens": u.get("output_tokens") or 0,
                    "cache_read": u.get("cache_read_input_tokens") or 0, "cost_usd": e.get("total_cost_usd"),
                    "at": now()}))
                return {"ok": ok, "text": text}
        else:  # codex exec --json
            if t == "thread.started" and e.get("thread_id"):
                self.s.set_agent(agent["id"], session_id=e["thread_id"])
            elif t in ("item.completed",):
                item = e.get("item") or {}
                it = item.get("type") or item.get("item_type")
                if it in ("agent_message", "assistant_message"):
                    txt = item.get("text") or ""
                    ev("assistant", txt)
                    self.s.set_agent(agent["id"], last_text=txt[-400:], last_activity=now())
                    self._last_codex = getattr(self, "_last_codex", {})
                    self._last_codex[turn] = txt
                elif it in ("command_execution",):
                    ev("tool", f"$ {item.get('command', '')[:300]} → exit {item.get('exit_code')}")
                elif it in ("file_change",):
                    ev("tool", "edit " + ", ".join(c.get("path", "") for c in item.get("changes") or [])[:300])
            elif t == "turn.completed":
                return {"ok": True, "text": getattr(self, "_last_codex", {}).get(turn, "")}
            elif t in ("turn.failed", "error"):
                msg = (e.get("error") or {}).get("message") if isinstance(e.get("error"), dict) else e.get("message")
                ev("error", str(msg)[:2000])
                return {"ok": False, "text": str(msg)}
        return None

    def _finish(self, agent, msgs, turn, result):
        state = "done" if result["ok"] else "failed"
        reply = result["text"] or ""
        with self.s.tx():
            self.s.db.execute("UPDATE messages SET state=?, finished_at=?, reply=?, error=? WHERE turn_id=? "
                              "AND state='delivering'",
                              (state, now(), reply[:20000], None if result["ok"] else reply[:2000], turn))
        if not result["ok"]:
            self.s.event(agent["id"], "error", reply[:2000], turn)
        self.s.set_agent(agent["id"], state="idle" if result["ok"] else "error", last_activity=now())
        getattr(self, "_procs", {}).pop(turn, None)
        if result["ok"]:
            self.route_replies(agent, msgs, reply)

    def route_replies(self, agent, msgs, reply):
        """Send the reply back to agent senders (one hop; replies never trigger replies)."""
        for m in msgs:
            sender = m["from_agent"]
            if sender in ("owner", "system") or m["is_reply"] or m.get("wait_reply") or m["hop"] >= 4 \
                    or not reply.strip():
                continue
            if not self.s.agent(sender):
                continue
            self.s.enqueue(sender, f"Reply from {agent['id']} to your message {m['id']}:\n\n{reply}",
                           frm=agent["id"], idem=f"reply:{m['id']}", hop=m["hop"] + 1, is_reply=True)

    def _kill(self, pid):
        try:
            os.killpg(int(pid), 15)
        except (OSError, ValueError, TypeError):
            pass

    def stop(self, agent_id):
        self.interrupt(agent_id)

    # ---------- observed panes ----------
    def pane_deliver(self, agent):
        """Type a one-line pointer into an idle pane, confirm it from the transcript."""
        if agent["state"] != "idle":
            return  # wait until the session is idle; the message stays queued
        if not agent.get("pane"):
            self._hold_queued(agent["id"], "no tmux pane for this session; adopt it or open it in a pane")
            return
        with self.s.tx():
            msgs = self.s.q("SELECT * FROM messages WHERE to_agent=? AND state='queued' ORDER BY created_at LIMIT 10",
                            (agent["id"],))
            if not msgs:
                return
            turn = new_id("p")
            ids = [m["id"] for m in msgs]
            self.s.db.execute(f"UPDATE messages SET state='delivering', turn_id=?, attempts=attempts+1, "
                              f"delivered_at=? WHERE id IN ({','.join('?' * len(ids))})", (turn, now(), *ids))
        full = with_image_paths(compose_prompt(msgs), attachment_paths(msgs))
        inbox_file = os.path.join(INBOX, f"{turn}.md")
        with open(inbox_file, "w") as f:
            f.write(full + "\n")
        senders = ", ".join(sorted({m["from_agent"] for m in msgs}))
        flat = " ".join(full.split())
        if len(flat) <= 300 and len(msgs) == 1 and msgs[0]["from_agent"] == "owner":
            line = flat
        else:
            line = (f"[switchboard {turn} from {senders}] {flat[:160]}… Full message: {inbox_file} "
                    f"(read it, then reply with `sb reply {turn} \"…\"`)")
        started = time.time()
        subprocess.run(["tmux", "send-keys", "-t", agent["pane"], "-l", line], check=True, timeout=10)
        time.sleep(0.4)
        subprocess.run(["tmux", "send-keys", "-t", agent["pane"], "Enter"], check=True, timeout=10)
        self.s.event(agent["id"], "user", full, turn)
        self.s.set_agent(agent["id"], state="busy", last_activity=now())
        self._pending_pane = getattr(self, "_pending_pane", {})
        self._pending_pane[turn] = {"agent": agent["id"], "line": line, "t0": started, "enter_retry": False}

    def poll_panes(self):
        pend = getattr(self, "_pending_pane", {})
        for turn, p in list(pend.items()):
            agent = self.s.agent(p["agent"])
            path = self._transcript_for(agent)
            if path and discovery.transcript_has_user_text(path, agent["provider"], p["line"], p["t0"]):
                self.s.x("UPDATE messages SET state='delivered' WHERE turn_id=? AND state='delivering'", (turn,))
                self.s.event(agent["id"], "system", f"{turn} landed in the session transcript", turn)
                pend.pop(turn)
                continue
            age = time.time() - p["t0"]
            if age > 20 and not p["enter_retry"]:
                screen = subprocess.run(["tmux", "capture-pane", "-p", "-t", agent["pane"]], capture_output=True,
                                        text=True).stdout
                if p["line"][:40] in " ".join(screen.split()):
                    subprocess.run(["tmux", "send-keys", "-t", agent["pane"], "Enter"], timeout=10)
                    self.s.event(agent["id"], "system", f"{turn}: text still in the composer, pressed Enter once",
                                 turn)
                p["enter_retry"] = True
            elif age > 90:
                self.s.x("UPDATE messages SET state='held', error=? WHERE turn_id=? AND state='delivering'",
                         ("not seen in the session transcript after 90 s; not re-typed (check the pane)", turn))
                self.s.event(agent["id"], "error", f"{turn} not confirmed; held, never re-typed", turn)
                pend.pop(turn)

    def _hold_queued(self, agent_id, why):
        self.s.x("UPDATE messages SET state='held', error=? WHERE to_agent=? AND state='queued'", (why, agent_id))

    def _transcript_for(self, agent):
        if not agent or not agent.get("session_id"):
            return None
        return discovery.transcript_path(agent["provider"], agent["session_id"])

    def close_observed_turns(self, agent):
        """An observed agent went idle after a delivered pane message: record its reply."""
        rows = self.s.q("SELECT * FROM messages WHERE to_agent=? AND state='delivered'", (agent["id"],))
        if not rows:
            return
        path = self._transcript_for(agent)
        reply = discovery.last_assistant_text(path, agent["provider"]) if path else ""
        self.s.x("UPDATE messages SET state='done', finished_at=?, reply=COALESCE(reply, ?) WHERE to_agent=? "
                 "AND state='delivered'", (now(), reply[:20000], agent["id"]))
        by_turn = {}
        for r in rows:
            by_turn.setdefault(r["turn_id"], []).append(r)
        for msgs in by_turn.values():
            self.route_replies(agent, msgs, reply)
