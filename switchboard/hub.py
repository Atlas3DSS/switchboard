"""Switchboard hub: HTTP API + dashboard + delivery loop. Run: python3 -m switchboard.hub"""
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, discovery, hosts, jobs, phone, sessions, shared, sources, transcript, ws
from .runner import Runner
from .terminals import PROVIDERS, NeedsConfirm, Terminals
from .store import STATE, Store, epoch, iso_from_epoch, new_id, now, save_upload, upload_path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
SEED = os.path.join(config.CONFIG_DIR, "projects.json")  # optional: projects to create on start (see examples/)
PANES = os.path.join(config.CONFIG_DIR, "panes.json")  # optional: tmux pane -> Codex thread id, when not inferable
PORT = int(os.environ.get("SB_PORT", "8802"))
# The hub has no login: it answers only requests addressed to this machine's loopback name (no DNS rebinding) and
# never a page from another site (Origin). SB_ALLOWED_HOSTS adds names, e.g. "box.tailnet.ts.net:8802" behind a proxy.
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"} | {
    h.strip().lower() for h in os.environ.get("SB_ALLOWED_HOSTS", "").split(",") if h.strip()}
SIMPLE_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data", "text/plain")
# Router (Switchboard chat) model per provider; the built-in agent uses its account's own model.
ROUTER_MODELS = {"claude": "claude-sonnet-5-5"}
STALE_S = 3 * 86400
SEND_TIMEOUT_S = 20  # one send to a browser terminal taking longer ends that connection (the page reconnects)


def code_fingerprint():
    """Hash of switchboard/*.py on disk; the UI compares it with the one this process started with."""
    import hashlib
    h = hashlib.sha1()
    pkg = os.path.join(ROOT, "switchboard")
    for name in sorted(os.listdir(pkg)):
        if name.endswith(".py"):
            h.update(name.encode())
            with open(os.path.join(pkg, name), "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:12]


CODE_AT_START = code_fingerprint()
STARTED_AT = time.time()


class Hub:
    def __init__(self, store=None):
        self.s = store or Store()
        self.r = Runner(self.s)
        self.t = Terminals(self.s)
        self.source_cache = {}
        self._model_checked = set()
        self.stop_flag = threading.Event()
        self.seed()
        self.ensure_project_dirs()
        self.focus = None  # {"agent", "project", "ts"}: POST /api/focus (e.g. from a desktop notifier) asks the page to open an agent
        self.t.on_waiting = self.waiting_changed
        self._ix = None  # past-session search index (sessions.db), opened on first use
        self.migrate_terminals()
        self.t.reattach_all()
        self.ensure_router()
        self.r.summary_fn = self.router_summary

    # ---------- seed / config ----------
    def migrate_terminals(self):
        """v2: every Claude/Codex agent becomes a terminal agent (launched lazily). Agents with a headless turn still
        running are converted by convert_managed once that turn ends. The front desk is retired."""
        if self.s.setting("terminals_v2"):
            return
        for a in self.s.agents():
            if a.get("role") == "desk":
                self.s.set_agent(a["id"], gone=1, state="stopped")
            elif a["provider"] in ("claude", "codex") and a["mode"] == "observed":
                self.s.set_agent(a["id"], mode="terminal", project_pinned=1)
        self.s.set_setting("terminals_v2", True)
        self.convert_managed()

    def router_account(self):
        """The account the Switchboard chat runs on: your choice in settings, else the first one that can run
        headless turns (Claude Code, Codex or an API account)."""
        accts = [a for a in discovery.accounts() if a["kind"] in ("claude", "codex", "api")]
        want = self.s.setting("router_account")
        return next((a for a in accts if a["name"] == want), None) or (accts[0] if accts else None)

    def ensure_router(self):
        """The Switchboard chat: a fast headless agent (id 'desk') whose only job is getting you to the right agent."""
        a = self.s.agent("desk")
        acct = self.router_account()
        if not acct:  # nothing set up yet; the setup page comes first
            if a and not a["gone"]:
                self.s.set_agent("desk", gone=1)
            return
        model = ROUTER_MODELS.get(acct["kind"]) if not acct.get("model") or acct["kind"] != "api" else acct["model"]
        upd = {"title": "Switchboard", "provider": acct["kind"], "account": acct["name"], "model": model,
               "mode": "managed", "role": "desk", "project": "switchboard", "gone": 0, "default_mode": "queue",
               "permission_mode": "auto", "project_pinned": 1, "cwd": os.path.join(STATE, "desk")}
        os.makedirs(upd["cwd"], exist_ok=True)
        if self.t.running("desk"):
            self.t.exit_cli("desk", interrupt=True)
        if not a:
            self.s.add_agent({"id": "desk", "state": "idle", "session_id": None, "last_activity": now(), **upd})
        elif any(a.get(k) != v for k, v in upd.items() if k != "gone") or a["gone"] or a["state"] == "stopped":
            self.s.set_agent("desk", session_id=None, context_tokens=0, state="idle", **upd)  # a fresh conversation

    def router_summary(self):
        """Very small: one line per project that has agents, one line for the rest."""
        agents = [a for a in self.s.agents() if a.get("role") != "desk" and a["provider"] != "shell"]
        lines, idle = ["PROJECTS (id - name: agents · next step)"], []
        for p in self.s.projects():
            if p["archived"]:
                continue
            mine = [a for a in agents if a["project"] == p["id"]]
            if not mine:
                idle.append(p["id"])
                continue
            ags = ", ".join(f"{a['id']} ({self.t.status(a)['state']}, {a['model'] or 'default'})" for a in mine)
            nxt = " ".join((p.get("next_step") or p.get("goal") or "").split())[:70]
            lines.append(f"- {p['id']} - {p['name']}: {ags} · {nxt}")
        if idle:
            lines.append("No agents yet: " + ", ".join(idle))
        return "\n".join(lines)

    def convert_managed(self):
        running = {r["to_agent"] for r in self.r.running_turns()}
        for a in self.s.agents():
            if a["mode"] == "managed" and a.get("role") != "desk" and a["id"] not in running:
                self.s.set_agent(a["id"], mode="terminal", state="idle")
                self.s.event(a["id"], "system", "now a terminal agent (opens on first use)")

    def seed(self):
        try:
            seed = json.load(open(SEED))
        except (OSError, ValueError):
            seed = []
        for p in seed:
            if not self.s.project(p["id"]):
                self.s.upsert_project(p)
            else:  # keep owner edits to text fields; refresh sources/match from the seed file
                self.s.upsert_project({"id": p["id"], "sources": p.get("sources", []), "match": p.get("match", {})})

    def project_dir(self, p):
        """The project's working folder: the first of its match.cwd dirs that exists."""
        return next((d for d in (os.path.expanduser(x) for x in ((p or {}).get("match") or {}).get("cwd") or [])
                     if os.path.isdir(d)), None)

    def ensure_project_dir(self, pid):
        """Give a project with no existing folder its own WORKSPACE/<id>, and return the project's folder."""
        p = self.s.project(pid)
        if not p:
            return None
        d = self.project_dir(p)
        if d:
            return d
        d = os.path.join(config.workspace(), discovery.slug(pid))
        os.makedirs(d, exist_ok=True)
        match = dict(p.get("match") or {})
        match["cwd"] = [d] + list(match.get("cwd") or [])
        self.s.upsert_project({"id": pid, "match": match})
        return d

    def ensure_project_dirs(self):
        for p in self.s.projects():
            if not p["archived"]:
                self.ensure_project_dir(p["id"])

    def pane_overrides(self):
        try:
            return json.load(open(PANES))
        except (OSError, ValueError):
            return {}

    # ---------- observed sessions ----------
    def project_for(self, cwd, pane_session=None, text=""):
        best, best_len = None, -1
        for p in self.s.projects():
            m = p.get("match") or {}
            if pane_session and pane_session in (m.get("panes") or []):
                return p["id"]
            for pre in m.get("cwd") or []:
                pre = os.path.expanduser(pre).rstrip("/")
                if cwd and (cwd == pre or cwd.startswith(pre + "/")) and len(pre) > best_len:
                    best, best_len = p["id"], len(pre)
        return best or "unsorted"

    def sync_observed(self):
        managed = {a["session_id"] for a in self.s.agents(include_gone=True) if a["mode"] != "observed"}
        live = discovery.claude_live_sessions()
        panes = discovery.tmux_panes()
        overrides = self.pane_overrides()
        seen = set()
        claimed_rollouts = set(overrides.values())
        rollouts = None

        self.follow_cleared(live)

        def upsert(aid, **kw):
            seen.add(aid)
            cur = self.s.agent(aid)
            if not cur:
                self.s.add_agent({"id": aid, "mode": "observed", "state": kw.get("state", "idle"), **kw})
                return
            if cur["mode"] != "observed":
                return
            upd = {k: v for k, v in kw.items() if not (k == "project" and cur["project_pinned"])}
            prev = cur["state"]
            upd["gone"] = 0
            self.s.set_agent(aid, **upd)
            if prev == "busy" and kw.get("state") == "idle":
                self.r.close_observed_turns(self.s.agent(aid))

        # Claude sessions (interactive in panes, interactive elsewhere, background)
        for sess in live:
            sid = sess.get("sessionId")
            if not sid or sid in managed:
                continue
            pane = next((p for p in panes if sess.get("pid") and sess["pid"] in p["pids"]), None)
            if not pane:
                pane = next((p for p in panes if p["claude_session"] == sid and p["kind"] == "claude"), None)
            aid = discovery.slug(pane["session"]) if pane else "c-" + discovery.slug(sess.get("name") or sid[:8])
            path = discovery.claude_transcript(sid)
            mtime = os.path.getmtime(path) if path else None
            status = sess.get("status") or sess.get("state") or "idle"
            state = "busy" if status in ("busy", "running") else ("held" if status == "blocked" else "idle")
            upsert(aid, project=self.project_for(sess.get("cwd"), pane and pane["session"]),
                   title=pane["session"] if pane else (sess.get("name") or aid), provider="claude",
                   account=(pane or {}).get("account") or sess.get("_account_dir"), session_id=sid,
                   cwd=sess.get("cwd"), pane=pane["target"] if pane else None, state=state,
                   last_activity=iso_from_epoch(mtime) if mtime else None,
                   last_text=(discovery.last_assistant_text(path, "claude")[-400:] if path else ""))
        # Codex panes
        for pane in panes:
            if pane["kind"] != "codex":
                continue
            aid = discovery.slug(pane["session"])
            thread = overrides.get(pane["target"]) or overrides.get(pane["session"])
            path = discovery.codex_rollout(thread) if thread else None
            if not path:
                if rollouts is None:
                    rollouts = discovery.recent_codex_rollouts(7)
                for m, p, meta in rollouts:
                    tid = meta.get("id")
                    if tid and tid not in claimed_rollouts and meta.get("cwd") == pane["cwd"] and \
                            meta.get("originator") == "codex-tui":
                        thread, path = tid, p
                        claimed_rollouts.add(tid)
                        break
            mtime = os.path.getmtime(path) if path else None
            state = "busy" if mtime and time.time() - mtime < 20 else "idle"
            upsert(aid, project=self.project_for(pane["cwd"], pane["session"]), title=pane["session"],
                   provider="codex", account=self.default_accounts().get("codex"), session_id=thread, cwd=pane["cwd"], pane=pane["target"],
                   state=state, last_activity=iso_from_epoch(mtime) if mtime else None,
                   last_text=(discovery.last_assistant_text(path, "codex")[-400:] if path else ""))
        for a in self.s.agents():
            if a["mode"] == "observed" and a["id"] not in seen:
                self.s.set_agent(a["id"], gone=1, state="stopped")

    def follow_cleared(self, live):
        """/clear (or /resume) inside a Claude terminal starts another session in the same process: follow it, so
        the agent's chat, usage and next launch use the new session instead of the old one."""
        by_pid = {s["pid"]: s.get("sessionId") for s in live if s.get("pid") and s.get("sessionId")}
        for a in self.s.agents():
            if a["mode"] != "terminal" or a["provider"] != "claude":
                continue
            pid = self.t.status(a)["pid"]
            sid = by_pid.get(pid)
            if not sid or sid == a["session_id"]:
                continue
            other = self.s.one("SELECT id FROM agents WHERE session_id=? AND id<>?", (sid, a["id"]))
            if other and self.s.agent(other["id"])["mode"] != "observed":
                continue
            if other:  # picked up as an observed session a moment ago
                self.s.set_agent(other["id"], gone=1, state="stopped", session_id=None)
            self.s.set_agent(a["id"], session_id=sid, context_tokens=0)
            self.s.event(a["id"], "system", f"now on session {sid} (cleared or switched in the terminal)")

    # ---------- past sessions ----------
    @property
    def ix(self):
        if self._ix is None:
            self._ix = sessions.Index()
        return self._ix

    def index_sessions(self):
        self.ix.refresh()

    def session_view(self, x):
        """A search hit plus what resuming it would do: open its agent, or add one to a project (maybe a new one)."""
        a = self.s.one("SELECT id, project FROM agents WHERE session_id=? AND gone=0", (x["id"],))
        pid = (a or {}).get("project") or self.project_for(x["cwd"])
        p = self.s.project(pid) if pid != "unsorted" else None
        return {**{k: v for k, v in x.items() if k != "path"}, "agent": (a or {}).get("id"),
                "project": p["id"] if p else None, "project_name": p["name"] if p else None}

    def search_sessions(self, q="", limit=30, headless=False):
        if not self.ix.count():
            self.ix.refresh()
        return {"sessions": [self.session_view(x) for x in self.ix.search(q, limit, headless)],
                "indexed": self.ix.count(), "indexing": self.ix.busy}

    def subagents(self, hours=72, model=None, agent=None, limit=20):
        """Subagents (Claude's Agent/Task tool) started recently, newest first, each with the agent or past
        session that spawned it."""
        parents = {}
        if agent:
            a = self.s.agent(agent)
            if not a:
                raise LookupError(agent)
            if not a["session_id"]:
                return {"subagents": []}
        out = []
        for x in discovery.subagents(hours, model):
            sid = x["parent_session"]
            if agent and sid != a["session_id"]:
                continue
            if sid not in parents:
                row = self.s.one("SELECT id, project FROM agents WHERE session_id=? ORDER BY gone LIMIT 1", (sid,))
                sess = None if row or self.ix.busy else self.ix.find(sid)  # never wait on a full re-index
                parents[sid] = {"agent": row["id"] if row else None, "project": row["project"] if row else None,
                                "session_title": (sess or {}).get("title") or (sess or {}).get("first_ask") or ""}
            out.append({**x, **parents[sid]})
            if len(out) >= limit:
                break
        return {"subagents": out}

    def relaunch(self, aid, b):
        """Restart an agent's CLI on the same session, optionally with another model or effort
        (like `codex --model X`). A headless agent takes them on its next turn."""
        a = self.s.agent(aid)
        if not a:
            raise LookupError(aid)
        if a.get("role") == "desk" or a["provider"] not in PROVIDERS or a["provider"] == "shell":
            raise ValueError("only agent CLIs can be relaunched (not the Switchboard chat or a shell)")
        upd = {k: (b[k] or None) for k in ("model", "effort") if k in b}
        if a["mode"] == "managed":
            self.s.set_agent(aid, **upd)
            self.s.event(aid, "system", "settings for the next turn: " + (", ".join(f"{k} {v}" for k, v in upd.items())
                                                                           or "unchanged"))
        elif a["mode"] == "terminal":
            self.t.relaunch(aid, upd, bool(b.get("confirm")))
        else:
            raise ValueError("this agent runs in a tmux pane the switchboard does not own; adopt it first")
        return self.agent_view(self.s.agent(aid))

    def resume_session(self, b):
        """Make a past session an agent (or find the agent it already is). With no matching project, a new
        project is made for it."""
        sid = (b.get("session_id") or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        x = self.ix.find(sid)
        if not x:
            raise LookupError(f"session {sid}")
        a = self.s.one("SELECT * FROM agents WHERE session_id=? AND gone=0", (x["id"],))
        if a:
            return {"agent": self.agent_view(a), "created": False, "created_project": False}
        cwd = x["cwd"] or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            raise ValueError(f"the session ran in {cwd}, which no longer exists; it can only be resumed from there")
        label = " ".join((b.get("title") or x["title"] or x["first_ask"] or "").split())
        pid, made = b.get("project"), False
        if pid and not self.s.project(pid):
            raise ValueError(f"unknown project {pid}")
        if not pid:
            pid = self.project_for(cwd)
        if pid == "unsorted":
            home = os.path.realpath(os.path.expanduser("~"))
            dirname = os.path.basename(cwd.rstrip("/")) if os.path.realpath(cwd) not in (home, "/") else ""
            name = (label[:60] if not dirname else dirname) or f"session {x['id'][:8]}"
            base = discovery.slug(name)[:32].strip("-") or "session"
            pid, n = base, 2
            while self.s.project(pid):
                pid, n = f"{base}-{n}", n + 1
            # a home-directory session gets no cwd rule: it would claim every unmatched session
            self.s.upsert_project({"id": pid, "name": name, "goal": label[:200],
                                   "next_step": "", "match": {"cwd": [cwd]} if dirname else {}})
            made = True
        agent = self.create_agent({"provider": x["provider"], "project": pid, "cwd": cwd, "session_id": x["id"],
                                   "title": b.get("title") or (label[:40] if label else x["id"][:8]),
                                   "account": b.get("account"), "model": b.get("model")})
        self.s.event(agent["id"], "system", f"resumed past session {x['id']} ({x['provider']}, last used {x['last_ts']})")
        return {"agent": agent, "created": True, "created_project": made}

    def clear_chat(self, aid):
        """Start the Switchboard chat over: stop a running turn, drop unsent messages, forget the session."""
        a = self.s.agent(aid)
        if not a:
            raise LookupError(aid)
        if a["mode"] != "managed":
            raise ValueError("type /clear in that agent's terminal; it follows the new session by itself")
        if self.s.one("SELECT 1 n FROM messages WHERE to_agent=? AND state IN ('delivering','delivered')", (aid,)):
            self.r.interrupt(aid, note="cleared by the owner")
        self.s.x("UPDATE messages SET state='interrupted', error='cleared by the owner', finished_at=COALESCE("
                 "finished_at, ?) WHERE to_agent=? AND state IN ('queued','held','failed')", (now(), aid))
        self.s.set_agent(aid, session_id=None, context_tokens=0, state="idle", last_text="")
        self.s.event(aid, "system", "conversation cleared by the owner")
        return self.agent_view(self.s.agent(aid))

    # ---------- shared project context (pulled on demand, one project only) ----------
    def transcript_of(self, a):
        if not a.get("session_id"):
            return None
        return discovery.transcript_path(a["provider"], a["session_id"])

    def context_scope(self, viewer, pid=None, aid=None):
        """The project a request may read. An agent reads only its own project; the owner reads any."""
        me = self.s.agent(viewer) if viewer and viewer != "owner" else None
        if aid:
            a = self.s.agent(aid)
            if not a or a["gone"]:
                raise LookupError(aid)
            pid = a["project"]
        pid = pid or (me or {}).get("project")
        if not pid:
            raise ValueError("which project? (--project)")
        if me and me["project"] != pid:
            raise PermissionError(f"{viewer} is on project {me['project']}; context is shared within a project only")
        return pid

    def project_agents(self, pid):
        return [a for a in self.s.agents() if a["project"] == pid and a.get("role") != "desk"
                and discovery.transcript_format(a["provider"])]

    def project_context(self, pid, viewer=None):
        pid = self.context_scope(viewer, pid)
        out = []
        for a in self.project_agents(pid):
            asked, said = shared.last_exchange(self.transcript_of(a), a["provider"])
            out.append({"id": a["id"], "title": a["title"], "provider": a["provider"], "model": a["model"],
                        "state": self.t.status(a)["state"], "last_activity": a["last_activity"],
                        "forked_from": (json.loads(a.get("fork_from") or "null") or {}).get("agent"),
                        "asked": asked, "said": said, "you": a["id"] == viewer})
        notes = self.s.q("SELECT * FROM notes WHERE project=? ORDER BY id DESC LIMIT 30", (pid,))
        return {"project": pid, "agents": out, "notes": list(reversed(notes))}

    def agent_context(self, aid, viewer=None, n=12, tools=False):
        self.context_scope(viewer, aid=aid)
        a = self.s.agent(aid)
        return {"agent": aid, "project": a["project"], "text": shared.digest(self.transcript_of(a), a["provider"],
                                                                              max(1, min(int(n), 200)), tools)}

    def context_search(self, pid, q, viewer=None, limit=20):
        pid = self.context_scope(viewer, pid)
        srcs = [(a["id"], self.transcript_of(a), a["provider"]) for a in self.project_agents(pid)]
        return {"project": pid, "hits": shared.search(srcs, q, max(1, min(int(limit), 100)))}

    def add_note(self, b):
        text = " ".join((b.get("text") or "").split())
        if not text:
            raise ValueError("text is required")
        if len(text) > 2000:
            raise ValueError("a note is at most 2000 characters; point to a file for more")
        frm = b.get("from") or "owner"
        pid = self.context_scope(frm, b.get("project"))
        self.s.x("INSERT INTO notes(project,agent,text,created_at) VALUES(?,?,?,?)", (pid, frm, text, now()))
        return self.project_context(pid)

    def summon(self, b):
        """A side agent beside one of the project's agents: the owner's /btw with a whole agent behind it.
        fork=True copies the source's conversation (Claude --fork-session, codex fork); otherwise the new agent
        starts fresh and pulls the source's context with `sb context` when it needs it."""
        src = self.s.agent(b.get("beside") or "")
        task = (b.get("task") or "").strip()
        if not src or src["gone"] or src["provider"] not in ("claude", "codex", "api", "gemini", "hermes"):
            raise ValueError("beside must be an agent (not a shell)")
        if not task:
            raise ValueError("task is required")
        fork = bool(b.get("fork")) and bool(src.get("session_id")) and src["provider"] in ("claude", "codex")
        if fork and not self.transcript_of(src):
            fork = False  # nothing said yet: there is nothing to copy
        a = self.create_agent({"provider": src["provider"], "project": src["project"], "cwd": src["cwd"],
                               "title": b.get("title") or f"{src['title'] or src['id']} side",
                               "account": b.get("account") or src["account"],
                               "model": b.get("model") or src["model"], "permission_mode": src.get("permission_mode"),
                               "effort": src.get("effort")})
        if fork:
            self.s.set_agent(a["id"], fork_from=json.dumps({"agent": src["id"], "session": src["session_id"]}),
                             session_id=str(uuid.uuid4()) if src["provider"] == "claude" else None)
            prompt = (f"[Side task from the owner. You are a fork of {src['id']}'s conversation; {src['id']} keeps "
                      f"working separately.]\n\n{task}")
        else:
            prompt = (f"[The owner summoned you beside {src['id']} on project {src['project']}. When you need to know "
                      f"what it is doing, pull it: `sb context {src['id']}` (add --n 30 or --tools for more).]\n\n{task}")
        self.s.enqueue(a["id"], prompt, frm=b.get("from") or "owner", idem=b.get("idem"))
        self.s.event(a["id"], "system", f"summoned beside {src['id']}" + (" (forked its conversation)" if fork else ""))
        return {"agent": self.agent_view(self.s.agent(a["id"])), "forked": fork}

    # ---------- sources ----------
    def probe_sources(self):
        for p in self.s.projects():
            out = []
            for src in p.get("sources") or []:
                try:
                    out.append(sources.probe(src))
                except Exception as e:
                    out.append({"kind": src.get("kind"), "label": src.get("label"), "path": src.get("path"),
                                "ok": False, "summary": f"probe error: {e}", "lines": [], "mtime": None})
            self.source_cache[p["id"]] = out

    # ---------- loops ----------
    def loop(self, fn, every):
        while not self.stop_flag.is_set():
            try:
                fn()
            except Exception:
                traceback.print_exc()
            self.stop_flag.wait(every)

    def delivery(self):
        self.r.tick()
        self.r.poll_panes()
        self.convert_managed()

    def terminal_tick(self):
        self.t.tick()

    def refresh_replies(self):
        """Newest assistant message per agent, straight from its transcript (both modes)."""
        for a in self.s.agents():
            if not a["session_id"]:
                continue
            path = discovery.transcript_path(a["provider"], a["session_id"])
            if not path:
                continue
            text, ts = transcript.last_reply(path, a["provider"])
            fresh = a["id"] not in self._model_checked
            self._model_checked.add(a["id"])
            if ts and (ts != a.get("last_reply_at") or fresh):
                self.s.set_agent(a["id"], last_reply_at=ts, last_text=text[-400:])
                if a["mode"] == "terminal" and a.get("role") != "desk":  # /model inside the terminal
                    m = discovery.last_model(path, a["provider"])
                    if m and m != a.get("model"):
                        self.s.set_agent(a["id"], model=m)
                        self.s.event(a["id"], "system", f"model is now {m} (changed in the terminal)")

    def refresh_usage(self):
        since = time.time() - 86400
        for a in self.s.agents():
            if not a["session_id"]:
                continue
            path = discovery.transcript_path(a["provider"], a["session_id"])
            if not path:
                continue
            u = transcript.usage_stats(path, a["provider"], since)
            upd = {"context_tokens": u["context_tokens"], "context_window": u["context_window"],
                   "tokens_24h": u["tokens_24h"]}
            prev = json.loads(a.get("last_turn") or "null")
            if u["last_turn"] and not (prev and prev.get("cost_usd") is not None and
                                       epoch(prev.get("at")) >= epoch(u["last_turn"]["at"]) - 5):
                upd["last_turn"] = json.dumps(u["last_turn"])
            self.s.set_agent(a["id"], **upd)

    def start(self):
        if os.environ.get("SB_DISCOVER") == "0":  # do not list Claude/Codex sessions started outside Switchboard
            self.sync_observed = lambda: None
        for fn, every in ((self.refresh_usage, 30), (self.delivery, 1.5), (self.sync_observed, 8), (self.probe_sources, 60),
                          (self.terminal_tick, 1.0), (self.refresh_replies, 5), (self.index_sessions, 120),
                          (self.fleet_tick, hosts.PROBE_S)):
            threading.Thread(target=self.loop, args=(fn, every), daemon=True).start()

    def fleet_tick(self):
        """Probe every host (docs/FLEET.md); track jobs; a host that missed a HALT gets the flag when it answers."""
        hosts.tick(self)
        jobs.tick(self)
        hosts.retry_pending_halt(self)

    # ---------- views ----------
    def lead_of(self, pid):
        p = self.s.project(pid) if pid else None
        if p and p.get("lead") and (self.s.agent(p["lead"]) or {}).get("gone") is False:
            return p["lead"]
        mine = [a["id"] for a in self.s.agents() if a["project"] == pid and a.get("role") != "desk"]
        return mine[0] if len(mine) == 1 else None

    def agent_view(self, a, leads=None):
        qr = self.s.one("SELECT COUNT(*) n, MIN(created_at) since, (SELECT error FROM messages WHERE to_agent=? AND "
                        "state='queued' ORDER BY created_at LIMIT 1) why FROM messages WHERE to_agent=? AND "
                        "state='queued'", (a["id"], a["id"]))
        q, why = qr["n"], qr["why"] or ""
        st = self.t.status(a)
        if a["mode"] == "managed":  # a headless turn from before v2 is still finishing
            st["state"] = a["state"]
        block = None if st["running"] else self.t.launch_block(a)
        term = self.t.term(a["id"]) if st["running"] else None
        asking = term.dialog() if term and a["provider"] != "shell" else None
        leads = leads if leads is not None else {a["project"]: self.lead_of(a["project"])}
        return {"id": a["id"], "project": a["project"], "title": a["title"], "provider": a["provider"],
                "account": a["account"], "model": a["model"],
                "session_id": a["session_id"], "cwd": a["cwd"], "mode": a["mode"],
                "lead": leads.get(a["project"]) == a["id"], "role": a.get("role"), "state": st["state"], "running": st["running"],
                "pid": st["pid"], "started_at": st["started_at"], "last_activity": a["last_activity"],
                "last_text": a["last_text"] or "", "launch_block": block, "queued": q, "paused": bool(a.get("paused")),
                "queued_since": qr["since"], "asking": asking,
                "queue_reason": why[len("waiting: "):] if why.startswith("waiting: ") else why or None,
                "switching": self.t.switching.get(a["id"]),
                "context_tokens": a.get("context_tokens"), "context_window": a.get("context_window")}

    def project_view(self, p, agents):
        srcs = self.source_cache.get(p["id"], [])
        times = [epoch(x["mtime"]) for x in srcs if x.get("mtime") and x.get("kind") != "cmd"]
        times += [epoch(a["last_activity"]) for a in agents if a["last_activity"]]
        last = max(times) if times else 0
        open_dec = len(self.s.decisions(p["id"], open_only=True))
        return {"id": p["id"], "name": p["name"], "goal": p["goal"], "next_step": p["next_step"],
                "paused": bool(p.get("paused")),
                "status_note": p["status_note"], "archived": p["archived"], "updated_at": p["updated_at"],
                "last_activity": iso_from_epoch(last) if last else None,
                "stale": bool(last) and time.time() - last > STALE_S and not p["archived"],
                "agent_ids": [a["id"] for a in agents], "open_decisions": open_dec, "lead": self.lead_of(p["id"]),
                "cwd": self.project_dir(p),
                "sources": p.get("sources") or []}

    def overview(self):
        raw = self.s.agents()
        leads = {p["id"]: self.lead_of(p["id"]) for p in self.s.projects()}
        agents = [self.agent_view(a, leads) for a in raw]
        projects = []
        for p in self.s.projects():
            mine = [a for a in agents if a["project"] == p["id"]]
            projects.append(self.project_view(p, mine))
        if any(a["project"] == "unsorted" for a in agents) and not self.s.project("unsorted"):
            projects.append({"id": "unsorted", "name": "Unsorted sessions", "goal": "Sessions not matched to a project",
                             "next_step": "", "status_note": "", "archived": False, "paused": False, "updated_at": None,
                             "last_activity": None, "stale": False, "open_decisions": 0, "sources": [], "lead": None,
                             "agent_ids": [a["id"] for a in agents if a["project"] == "unsorted"]})
        accts = [{**{k: x[k] for k in ("name", "kind", "label", "model", "auth")},
                  "agents": [a["id"] for a in agents if a["account"] == x["name"]]} for x in discovery.accounts()]
        disk = code_fingerprint()
        code = {"running": CODE_AT_START, "disk": disk, "stale": disk != CODE_AT_START,
                "started_at": iso_from_epoch(STARTED_AT)}
        return {"ts": now(), "code": code, "router": "desk" if self.router_account() else None,
                "setup_needed": not config.configured(), "focus": self.focus, "projects": projects, "agents": agents, "accounts": accts,
                "fleet": hosts.summary(self), "decisions": self.s.decisions(open_only=True)}

    def msg_view(self, m):
        return {"id": m["id"], "to": m["to_agent"], "from": m["from_agent"], "body": m["body"], "state": m["state"],
                "attempts": m["attempts"], "created_at": m["created_at"], "delivered_at": m["delivered_at"],
                "finished_at": m["finished_at"], "reply": m["reply"], "error": m["error"], "hop": m["hop"],
                "mode": m.get("mode") or "queue",
                "attachments": [{"id": u, "url": f"/api/uploads/{u}", "media_type": upload_path(u)[1]}
                                for u in json.loads(m.get("attachments") or "[]")]}

    def project_detail(self, pid):
        if pid == "unsorted" and not self.s.project("unsorted"):
            p = {"id": "unsorted", "name": "Unsorted sessions", "goal": "Sessions not matched to a project",
                 "next_step": "", "status_note": "", "archived": False, "updated_at": None, "sources": []}
        else:
            p = self.s.project(pid)
        if not p:
            return None
        agents = [self.agent_view(a) for a in self.s.agents() if a["project"] == pid]
        ids = [a["id"] for a in agents]
        msgs = []
        for aid in ids:
            msgs += self.s.messages(agent=aid, limit=50)
        msgs = sorted({m["id"]: m for m in msgs}.values(), key=lambda m: m["created_at"], reverse=True)[:50]
        return {"project": self.project_view(p, agents), "agents": agents,
                "sources": self.source_cache.get(pid, []), "messages": [self.msg_view(m) for m in msgs[:20]],
                "decisions": self.s.decisions(pid)}

    # ---------- actions ----------
    def send(self, body):
        to, text = body.get("to"), (body.get("body") or "").strip()
        attachments = [a for a in (body.get("attachments") or []) if isinstance(a, str)]
        for uid in attachments:
            if not upload_path(uid)[0]:
                raise ValueError(f"unknown upload {uid}")
        if not to or not (text or attachments):
            raise ValueError("to and a body or an attachment are required")
        agent = self.s.agent(to)
        if not agent and self.s.project(to):
            lead = self.lead_of(to)
            if not lead:
                raise ValueError(f"project {to} has no lead agent; set one on its card")
            to, agent = lead, self.s.agent(lead)
        if not agent or agent["gone"]:
            raise LookupError(f"unknown agent {to}")
        if agent["provider"] == "shell":
            raise ValueError(f"{to} is a plain shell; messages go to agents")
        hop = int(body.get("hop") or 0)
        if hop > 6:
            raise PermissionError(f"hop limit: this message is {hop} agent hops from the owner; stop and report")
        mode = body.get("mode") if body.get("mode") in ("queue", "inject", "interrupt") else \
            (agent.get("default_mode") or "queue")
        if agent["mode"] == "terminal":
            mode = "queue"  # typed into the terminal once its composer is empty
        m, dup = self.s.enqueue(to, text, frm=body.get("from") or "owner", idem=body.get("idem"),
                                hop=hop, wait_reply=bool(body.get("wait")), attachments=attachments, mode=mode)
        if not dup and m["state"] == "queued":
            if mode == "inject" and not self.r.inject(m):
                self.s.x("UPDATE messages SET mode='queue' WHERE id=?", (m["id"],))
            elif mode == "interrupt":
                self.r.interrupt(to, note="stopped by the owner to send a new message")
            m = self.s.message(m["id"])
        return {"message": self.msg_view(m), "duplicate": dup}

    def default_accounts(self):
        d = dict(self.s.setting("default_account", {}) or {})
        kinds = {}
        for x in discovery.accounts():
            kinds.setdefault(x["kind"], []).append(x["name"])
        for prov, names in kinds.items():
            if d.get(prov) not in names:
                d[prov] = names[0]
        return d

    def switch_account(self, aid, account, confirm=False):
        a = self.s.agent(aid)
        if not a:
            raise LookupError(aid)
        kinds = {x["name"]: x["kind"] for x in discovery.accounts()}
        if kinds.get(account) != a["provider"]:
            raise ValueError(f"{account} is not a {a['provider']} account")
        if a["mode"] == "terminal":
            self.t.switch_account(aid, account, confirm)
        elif a["mode"] == "managed":
            self.s.set_agent(aid, account=account, account_pending=None)
            self.s.event(aid, "system", f"account switched to {account} (next turn)")
        else:
            self.s.set_agent(aid, account_pending=None if account == a["account"] else account)
            self.s.event(aid, "system", f"account {account} applies when this tab moves to the switchboard")
        return self.agent_view(self.s.agent(aid))

    def create_agent(self, b):
        provider = b.get("provider")
        if not provider and b.get("account"):
            provider = next((a["kind"] for a in discovery.accounts() if a["name"] == b["account"]), None)
        if provider not in PROVIDERS:
            raise ValueError("provider must be one of " + ", ".join(PROVIDERS))
        acct = None if provider == "shell" else (b.get("account") or self.default_accounts().get(provider))
        if provider != "shell" and acct not in {a["name"] for a in discovery.accounts() if a["kind"] == provider}:
            raise ValueError(f"no {provider} account named {acct}; add one on the Setup page" if acct else
                             f"no {provider} account yet; add one on the Setup page")
        pid = b.get("project")
        if b.get("cwd"):
            cwd = os.path.expanduser(b["cwd"])
        else:  # no folder given: the project's own folder, else the workspace root
            cwd = (self.ensure_project_dir(pid) if pid and pid != "unsorted" else None) or config.workspace()
            os.makedirs(cwd, exist_ok=True)
        if not os.path.isdir(cwd):
            raise ValueError(f"cwd {cwd} does not exist")
        title = b.get("title") or ("shell" if provider == "shell" else "agent")
        base = discovery.slug(b.get("id") or title)
        aid, n = base, 2
        while self.s.agent(aid):
            aid, n = f"{base}-{n}", n + 1
        sid = (b.get("session_id") or "").strip() or None
        if sid and not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
            raise ValueError("session id should look like 12345678-abcd-…")
        if sid and self.s.one("SELECT id FROM agents WHERE session_id=? AND gone=0", (sid,)):
            raise ValueError(f"session {sid} already belongs to agent "
                             f"{self.s.one('SELECT id FROM agents WHERE session_id=? AND gone=0', (sid,))['id']}")
        a = self.s.add_agent({"id": aid, "project": pid or self.project_for(cwd), "title": title,
                              "provider": provider, "account": acct, "model": b.get("model") or None,
                              "mode": "terminal", "session_id": None if provider == "shell" else (sid or (str(uuid.uuid4()) if provider in ("claude", "api") else None)),
                              "cwd": cwd, "permission_mode": b.get("permission_mode") or None,
                              "effort": b.get("effort") or None, "state": "idle", "project_pinned": 1,
                              "last_activity": now()})
        if b.get("prompt"):
            self.s.enqueue(aid, b["prompt"], frm=b.get("from") or "owner", idem=b.get("idem"))
        return self.agent_view(a)

    def adopt(self, aid):
        a = self.s.agent(aid)
        if not a:
            raise LookupError(aid)
        if a["mode"] == "managed":
            return self.agent_view(a)
        if a["provider"] == "claude":
            live = [x for x in discovery.claude_live_sessions() if x.get("sessionId") == a["session_id"]
                    and x.get("pid") and discovery.pid_alive(x["pid"])]
            if live:
                raise PermissionError("the session is still open in a terminal; exit it there (/exit), then adopt")
        elif a["pane"]:
            raise PermissionError("close the Codex TUI in its pane first, then adopt")
        if not a["session_id"]:
            raise ValueError("no session id known for this agent")
        self.s.set_agent(aid, mode="managed", pane=None, state="idle", gone=0, project_pinned=1)
        self.s.x("UPDATE messages SET state='queued', error=NULL WHERE to_agent=? AND state='held'", (aid,))
        self.s.event(aid, "system", "adopted: switchboard now delivers turns headlessly")
        return self.agent_view(self.s.agent(aid))

    def restart(self):
        """Re-exec the hub on the code now on disk, once that code imports cleanly. Terminals (pty holders) and
        headless turns are separate processes and carry on; pages reconnect by themselves."""
        chk = subprocess.run([sys.executable, "-c", "import switchboard.hub"], cwd=ROOT, capture_output=True,
                             text=True, timeout=60)
        if chk.returncode:
            err = (chk.stderr or chk.stdout).strip().splitlines()
            raise PermissionError("not restarting: the code on disk does not load: " + " | ".join(err[-3:]))

        def reexec():
            self.stop_flag.set()
            os.chdir(ROOT)
            os.execv(sys.executable, [sys.executable, "-m", "switchboard.hub"])

        threading.Timer(0.5, reexec).start()
        return {"ok": True, "running": CODE_AT_START, "disk": code_fingerprint()}

    # ---------- notifications (the page's inbox) ----------
    def notify(self, agent, text, kind="notify", level="need", idem=None, project=None):
        """Something for the owner. level 'need' = they have to act; 'info' = FYI (expires after an hour)."""
        text = " ".join((text or "").split())[:300]
        if not text:
            raise ValueError("text is required")
        a = self.s.agent(agent) if agent else None
        project = project or (a or {}).get("project")
        if idem and self.s.one("SELECT 1 n FROM notifications WHERE idem=?", (idem,)):
            return self.s.one("SELECT * FROM notifications WHERE idem=?", (idem,))
        nid = new_id("n")
        self.s.x("INSERT INTO notifications(id,idem,agent,project,kind,level,text,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (nid, idem or nid, agent, project, kind, "info" if level == "info" else "need", text, now()))
        return self.s.one("SELECT * FROM notifications WHERE id=?", (nid,))

    def notifications(self):
        self.s.x("UPDATE notifications SET acked_at=? WHERE acked_at IS NULL AND level='info' AND created_at<?",
                 (now(), iso_from_epoch(time.time() - 3600)))
        rows = self.s.q("SELECT * FROM notifications WHERE acked_at IS NULL ORDER BY (level='need') DESC, created_at")
        return {"items": rows, "need": sum(1 for r in rows if r["level"] == "need")}

    def ack(self, nid=None, where=None, args=()):
        if nid:
            self.s.x("UPDATE notifications SET acked_at=? WHERE id=? AND acked_at IS NULL", (now(), nid))
        else:
            self.s.x(f"UPDATE notifications SET acked_at=? WHERE acked_at IS NULL AND {where or '1=1'}", (now(), *args))

    def waiting_changed(self, aid, waiting, why=""):
        """A terminal is stuck on a question only the owner can answer (permission or trust prompt), or no longer."""
        if waiting:
            if not self.s.one("SELECT 1 n FROM notifications WHERE agent=? AND kind='waiting' AND acked_at IS NULL", (aid,)):
                self.notify(aid, f"is waiting for you in its terminal: {why}", kind="waiting")
        else:
            self.ack(where="agent=? AND kind='waiting'", args=(aid,))

    def set_focus(self, agent):
        a = self.s.agent(agent)
        if not a:
            raise LookupError(agent)
        self.focus = {"agent": a["id"], "project": a["project"], "ts": now()}
        return self.focus

    def dispatch(self, b):
        """Start an agent on a project with one task (`sb dispatch`, e.g. from a watchdog script or another agent).
        The same idem never starts a second agent."""
        pid, task = b.get("project"), (b.get("task") or "").strip()
        p = self.s.project(pid) if pid else None
        if not p or not task:
            raise ValueError("project (an existing project id) and task are required")
        if b.get("idem"):
            m = self.s.one("SELECT to_agent FROM messages WHERE idem=?", (b["idem"],))
            if m and self.s.agent(m["to_agent"]):
                return {"agent": self.agent_view(self.s.agent(m["to_agent"])), "duplicate": True}
        acct = b.get("account") or (self.router_account() or {}).get("name")
        a = self.create_agent({"project": pid, "cwd": b.get("cwd"), "title": b.get("title") or f"task-{pid}",
                               "model": b.get("model"), "account": acct,
                               "permission_mode": "auto", "prompt": task, "idem": b.get("idem"),
                               "from": b.get("from") or "owner"})
        self.s.event(a["id"], "system", f"dispatched by {b.get('from') or 'owner'}")
        self.notify(a["id"], f"agent dispatched on {pid} by {b.get('from') or 'owner'}: {task[:160]}",
                    kind="dispatch", level="info", idem=f"dispatch:{b.get('idem') or a['id']}")
        return {"agent": a, "duplicate": False}

    # ---------- setup (your accounts) ----------
    def setup_view(self):
        cfg = config.load()
        ra = self.router_account()
        return {"configured": bool(cfg["accounts"]), "workspace": cfg.get("workspace") or config.DEFAULT_WORKSPACE,
                "accounts": [config.public(a) for a in cfg["accounts"]], "presets": config.PRESETS,
                "router_account": ra["name"] if ra else None, "detect": config.detect()}

    def save_setup(self, b):
        items = b.get("accounts")
        if not isinstance(items, list):
            raise ValueError("accounts must be a list")
        for a in items:  # "use the key in $OPENAI_API_KEY" from the setup page
            if not a.get("api_key") and a.get("key_env"):
                a["api_key"] = config.env_key(a["key_env"])
        config.set_accounts(items, b.get("workspace"))
        if b.get("router_account"):
            self.s.set_setting("router_account", b["router_account"])
        os.makedirs(config.workspace(), exist_ok=True)
        self.ensure_router()
        return self.setup_view()

    def answer(self, did, answer):
        d = self.s.decision(did)
        if not d:
            raise LookupError(did)
        if d["state"] != "open":
            return d
        self.s.x("UPDATE decisions SET state='answered', answer=?, answered_at=? WHERE id=?", (answer, now(), did))
        self.ack(where="idem=?", args=(f"decision:{did}",))
        if d["agent"] and self.s.agent(d["agent"]):
            self.s.enqueue(d["agent"], f"Owner answered your question {did} (\"{d['question']}\"): {answer}",
                           frm="owner", idem=f"answer:{did}")
        return self.s.decision(did)


    def terminal_session(self, sock, a, start):
        """One browser terminal: replay scrollback, stream output, forward keys and resizes."""
        import queue
        aid = a["id"]
        out = queue.Queue(maxsize=4096)

        def status(**kw):
            st = self.t.status(self.s.agent(aid))
            return json.dumps({"t": "status", "state": st["state"], "running": st["running"], **kw})

        if start and not self.t.running(aid):
            if a["mode"] == "managed":
                self.convert_managed()
                a = self.s.agent(aid)
            try:
                self.t.launch(a)
            except PermissionError as e:
                sock.send(self.t.scrollback(aid))
                sock.send(json.dumps({"t": "blocked", "reason": str(e)}))
            except Exception as e:
                sock.send(json.dumps({"t": "blocked", "reason": f"launch failed: {e}"}))
        t = self.t.term(aid)
        behind = threading.Event()

        def watchdog():  # a browser that stops reading is cut off: never a writer stuck in send forever
            while not sock.closed:
                time.sleep(1)
                if behind.is_set() or (sock.sending_since and time.time() - sock.sending_since > SEND_TIMEOUT_S):
                    sock.abort()

        threading.Thread(target=watchdog, daemon=True).start()

        def listener(kind, payload):
            if behind.is_set():
                return
            if kind == "resync":  # we reattached to the holder: the page reconnects and gets the full scrollback
                behind.set()
                return
            try:
                out.put_nowait((kind, payload))
            except queue.Full:
                behind.set()  # never skip bytes: close it, the page reconnects and gets the scrollback again

        def writer():
            while not sock.closed:
                if behind.is_set():
                    sock.abort()
                    return
                try:
                    kind, payload = out.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    if kind == "o":
                        sock.send(payload)
                    else:
                        sock.send(json.dumps({"t": "exit", "code": payload}))
                except (OSError, ConnectionError):
                    return

        try:  # from the first replay on: whatever happens, the listener goes and the socket is closed
            if t:
                with t.lock:
                    replay = bytes(t.buf)
                    t.listeners.add(listener)
                for i in range(0, len(replay), 256 * 1024):
                    sock.send(replay[i:i + 256 * 1024])
                sock.send(status())
            elif not start or self.t.launch_block(self.s.agent(aid)) is None:
                sock.send(self.t.scrollback(aid))
                sock.send(status())
            threading.Thread(target=writer, daemon=True).start()
            while True:
                op, data = sock.recv()
                if op != ws.TEXT:
                    continue
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                cur = self.t.term(aid)
                if not cur:
                    continue
                if t is not cur:  # the CLI was relaunched (account switch): follow the new one
                    if t:
                        t.listeners.discard(listener)
                    t = cur
                    with t.lock:
                        t.listeners.add(listener)
                try:
                    if msg.get("t") == "i" and isinstance(msg.get("d"), str):
                        cur.write(msg["d"])
                    elif msg.get("t") == "r":
                        cur.resize(msg.get("cols") or 120, msg.get("rows") or 40)
                except (OSError, ConnectionError, ValueError):
                    pass
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            if t:
                t.listeners.discard(listener)
            sock.closed = True


# ---------------- HTTP ----------------
def make_handler(hub):
    class H(BaseHTTPRequestHandler):
        server_version = "switchboard/0"

        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")

        def _bytes(self, data, media):
            self.send_response(200)
            self.send_header("Content-Type", media or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return None

        def _refuse(self, write=False):
            """Why this request must not be served (another site's page, a rebound DNS name), or None."""
            host = (self.headers.get("Host") or "").lower()
            if host not in ALLOWED_HOSTS:
                return f"host {host or '?'} is not allowed (SB_ALLOWED_HOSTS)"
            origin = (self.headers.get("Origin") or "").lower()
            if origin and urlparse(origin).netloc not in ALLOWED_HOSTS:
                return f"requests from {origin} are not allowed"
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if write and ctype in SIMPLE_TYPES:  # a cross-site form could send these without a CORS preflight
                return "send JSON (Content-Type: application/json)"
            return None

        def _guard(self, fn):
            why = self._refuse(write=self.command in ("POST", "PATCH", "DELETE"))
            if why:
                return self._json(403, {"error": why})
            try:
                res = fn()
                if res is None:
                    return
                code, obj = res
                self._json(code, obj)
            except NeedsConfirm as e:
                self._json(409, {"error": str(e), "needs_confirm": True})
            except LookupError as e:
                self._json(404, {"error": f"not found: {e}"})
            except PermissionError as e:
                self._json(409, {"error": str(e)})
            except (ValueError, KeyError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        def do_GET(self):
            u = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path
            m = re.fullmatch(r"/ws/agents/([\w.-]+)", path)
            if m:
                why = self._refuse()
                if why:
                    return self._json(403, {"error": why})
                return self._terminal_ws(m.group(1), qs.get("start") != "0")
            if not path.startswith("/api/"):
                if self._refuse():  # a rebound name never gets the page either
                    return self._json(403, {"error": self._refuse()})
                return self._static(path)

            def route():
                if path == "/api/overview":
                    return 200, hub.overview()
                if path == "/api/notifications":
                    return 200, hub.notifications()
                if path == "/api/setup":
                    return 200, hub.setup_view()
                if path == "/api/phone":  # never reachable through the gate (it refuses /api/phone*)
                    return 200, phone.status()
                if path == "/api/subagents":
                    return 200, hub.subagents(max(1, min(float(qs.get("hours", 72)), 24 * 60)), qs.get("model"),
                                              qs.get("agent"), max(1, min(int(qs.get("limit", 20)), 200)))
                if path == "/api/sessions":
                    return 200, hub.search_sessions(qs.get("q", ""), int(qs.get("limit", 30)),
                                                    qs.get("headless") in ("1", "true"))
                m = re.fullmatch(r"/api/projects/([\w.-]+)/context", path)
                if m:
                    if qs.get("q"):
                        return 200, hub.context_search(m.group(1), qs["q"], qs.get("from"), qs.get("limit", 20))
                    return 200, hub.project_context(m.group(1), qs.get("from"))
                if path == "/api/context":  # an agent's own project: sb context without --project
                    if qs.get("q"):
                        return 200, hub.context_search(None, qs["q"], qs.get("from"), qs.get("limit", 20))
                    return 200, hub.project_context(None, qs.get("from"))
                m = re.fullmatch(r"/api/agents/([\w.-]+)/context", path)
                if m:
                    return 200, hub.agent_context(m.group(1), qs.get("from"), qs.get("n", 12),
                                                  qs.get("tools") in ("1", "true"))
                m = re.fullmatch(r"/api/projects/([\w.-]+)", path)
                if m:
                    d = hub.project_detail(m.group(1))
                    if not d:
                        raise LookupError(m.group(1))
                    return 200, d
                m = re.fullmatch(r"/api/agents/([\w.-]+)/chat", path)
                if m:
                    a = hub.s.agent(m.group(1))
                    if not a:
                        raise LookupError(m.group(1))
                    p = discovery.transcript_path(a["provider"], a["session_id"])
                    chat = transcript.read_chat(p, a["provider"], int(qs.get("limit", 40)),
                                                qs.get("before"), qs.get("after"))
                    out = []
                    for ent in chat["entries"]:
                        n = ent.pop("n_images", 0)
                        ent["images"] = [{"url": f"/api/agents/{a['id']}/image?entry={ent['id']}&n={k}"}
                                         for k in range(n)]
                        if ent["role"] != "user":
                            out.append(ent)
                            continue
                        for k, (frm, reply, text) in enumerate(transcript.senders(ent["text"])):  # who said it
                            out.append({**ent, "text": text, "from": frm, "reply": reply, "part": k,
                                        "images": ent["images"] if k == 0 else []})
                    chat["entries"] = out
                    return 200, chat
                m = re.fullmatch(r"/api/agents/([\w.-]+)/image", path)
                if m:
                    a = hub.s.agent(m.group(1))
                    if not a or not a["session_id"]:
                        raise LookupError(m.group(1))
                    p = discovery.transcript_path(a["provider"], a["session_id"])
                    data, media = transcript.image_at(p, a["provider"], qs.get("entry"), qs.get("n", 0))
                    return self._bytes(data, media)
                m = re.fullmatch(r"/api/uploads/([\w.-]+)", path)
                if m:
                    p, media = upload_path(m.group(1))
                    if not p:
                        raise LookupError(m.group(1))
                    return self._bytes(open(p, "rb").read(), media)
                m = re.fullmatch(r"/api/agents/([\w.-]+)/events", path)
                if m:
                    return 200, {"events": hub.s.events(m.group(1), int(qs.get("after", 0)),
                                                        min(int(qs.get("limit", 200)), 1000))}
                m = re.fullmatch(r"/api/agents/([\w.-]+)", path)
                if m:
                    a = hub.s.agent(m.group(1))
                    if not a:
                        raise LookupError(m.group(1))
                    return 200, hub.agent_view(a)
                if path == "/api/fleet":  # the inventory: every host, its capacity, jobs, untracked work (docs/FLEET.md)
                    return 200, hosts.fleet_view(hub)
                if path == "/api/fleet/guards":
                    return 200, hosts.guards(hub, "list", qs["host"].split(",") if qs.get("host") else None)
                if path == "/api/fleet/wsl-keepalive":
                    return 200, {"text": hosts.wsl_keepalive(qs.get("distro") or "Ubuntu")}
                if path == "/api/jobs":  # sb jobs
                    return 200, jobs.list_jobs(hub, qs.get("all") == "1", qs.get("project"), qs.get("host"), qs.get("agent"))
                m = re.fullmatch(r"/api/jobs/([\w-]+)", path)
                if m:
                    return 200, jobs.show(hub, m.group(1), int(qs.get("log", 40)))
                if path == "/api/agents":
                    return 200, {"agents": [hub.agent_view(a) for a in hub.s.agents()]}
                if path == "/api/messages":
                    return 200, {"messages": [hub.msg_view(x) for x in hub.s.messages(
                        qs.get("agent"), qs.get("state"), min(int(qs.get("limit", 50)), 500))]}
                m = re.fullmatch(r"/api/messages/([\w.-]+)", path)
                if m:
                    x = hub.s.message(m.group(1))
                    if not x:
                        raise LookupError(m.group(1))
                    return 200, hub.msg_view(x)
                raise LookupError(path)

            self._guard(route)

        def do_POST(self):
            path = urlparse(self.path).path

            def route():
                if path == "/api/uploads":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 15 * 1024 * 1024:
                        raise ValueError("image must be 15 MB or smaller")
                    media = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    return 200, save_upload(self.rfile.read(n), media)
                b = self._body()
                m = re.fullmatch(r"/api/agents/([\w.-]+)/interrupt", path)
                if m:
                    if not hub.s.agent(m.group(1)):
                        raise LookupError(m.group(1))
                    hub.r.interrupt(m.group(1))
                    return 200, hub.agent_view(hub.s.agent(m.group(1)))
                if path == "/api/messages":
                    return 200, hub.send(b)
                m = re.fullmatch(r"/api/messages/([\w.-]+)/retry", path)
                if m:
                    x = hub.s.message(m.group(1))
                    if not x:
                        raise LookupError(m.group(1))
                    if x["state"] in ("failed", "held"):
                        hub.s.x("UPDATE messages SET state='queued', error=NULL, turn_id=NULL WHERE id=?", (x["id"],))
                    return 200, hub.msg_view(hub.s.message(x["id"]))
                if path == "/api/fleet/halt":  # sb halt: flag every host, stop tracked jobs, --workloads disables matches
                    return 200, hosts.halt(hub, b.get("from") or "owner", b.get("reason") or "", b.get("hosts") or None,
                                           bool(b.get("workloads")))
                if path == "/api/fleet/resume":
                    return 200, hosts.resume(hub, b.get("from") or "owner", b.get("hosts") or None)
                if path == "/api/fleet/guards":
                    act = b.get("action") or "install"
                    if act not in ("install", "remove"):
                        raise ValueError("action is install or remove")
                    return 200, hosts.guards(hub, act, b.get("hosts") or None)
                if path == "/api/fleet/probe":  # sb fleet check: probe now instead of waiting for the loop
                    return 200, {"results": hosts.tick(hub), "fleet": hosts.fleet_view(hub)}
                if path == "/api/fleet/hosts":  # sb fleet add-host: probe first, then write the hosts file
                    return 200, hosts.add_host(b)
                if path == "/api/jobs":  # sb run: the one way to launch work (docs/FLEET.md)
                    return 200, jobs.submit(hub, b)
                if path == "/api/jobs/adopt":
                    return 200, jobs.adopt(hub, b)
                m = re.fullmatch(r"/api/jobs/([\w-]+)/stop", path)
                if m:
                    return 200, jobs.stop(hub, m.group(1), b.get("from") or "owner", b.get("reason") or "")
                if path == "/api/agents":
                    return 200, hub.create_agent(b)
                if path == "/api/dispatch":
                    return 200, hub.dispatch(b)
                if path == "/api/sessions/resume":
                    return 200, hub.resume_session(b)
                m = re.fullmatch(r"/api/agents/([\w.-]+)/clear", path)
                if m:
                    return 200, hub.clear_chat(m.group(1))
                if path == "/api/notifications":
                    return 200, hub.notify(b.get("agent"), b.get("text"), "notify", b.get("level") or "need", b.get("idem"))
                if path == "/api/notifications/ack-all":
                    hub.ack()
                    return 200, hub.notifications()
                m = re.fullmatch(r"/api/notifications/([\w.-]+)/ack", path)
                if m:
                    hub.ack(m.group(1))
                    return 200, hub.notifications()
                if path == "/api/focus":
                    return 200, hub.set_focus(b.get("agent"))
                if path == "/api/restart":
                    return 200, hub.restart()
                if path == "/api/notes":
                    return 200, hub.add_note(b)
                if path == "/api/summon":
                    return 200, hub.summon(b)
                m = re.fullmatch(r"/api/agents/([\w.-]+)/compact", path)
                if m:
                    a = hub.s.agent(m.group(1))
                    if not a:
                        raise LookupError(m.group(1))
                    if a["mode"] != "managed" or a["provider"] != "claude":
                        raise ValueError("compact works on Claude agents that are on the switchboard")
                    return 200, hub.send({"to": a["id"], "body": "/compact", "idem": b.get("idem")})
                if path == "/api/setup":
                    return 200, hub.save_setup(b)
                if path == "/api/setup/test":
                    a = b.get("account") or {}
                    if a.get("kind", "api") != "api":
                        return 200, {"ok": True, "detect": config.detect()}
                    if not a.get("api_key") and a.get("key_env"):
                        a["api_key"] = config.env_key(a["key_env"])
                    return 200, config.test_api(a)
                if path == "/api/settings":
                    if "default_account" in b:
                        kinds = {x["name"]: x["kind"] for x in discovery.accounts()}
                        cur = hub.default_accounts()
                        for prov, name in (b["default_account"] or {}).items():
                            if kinds.get(name) != prov:
                                raise ValueError(f"{name} is not a {prov} account")
                            cur[prov] = name
                        hub.s.set_setting("default_account", cur)
                    return 200, {"default_account": hub.default_accounts()}
                if path == "/api/phone/pair":  # a one-time code for the QR (from this computer only)
                    if b.get("origin"):
                        phone.set_origin(b["origin"])
                    if not phone.origin():
                        raise ValueError("set the phone address first (https://<your hostname>)")
                    token, ttl = phone.new_pairing()
                    return 200, {"url": f"{phone.origin()}/gate/#pair={token}", "expires_in": ttl, **phone.status()}
                if path == "/api/phone/unpair":
                    phone.unpair()
                    return 200, phone.status()
                m = re.fullmatch(r"/api/agents/([\w.-]+)/relaunch", path)
                if m:
                    return 200, hub.relaunch(m.group(1), b)
                m = re.fullmatch(r"/api/agents/([\w.-]+)/(start|stop|account)", path)
                if m:
                    aid, act = m.groups()
                    a = hub.s.agent(aid)
                    if not a:
                        raise LookupError(aid)
                    if act == "start":
                        hub.t.launch(a)
                    elif act == "stop":
                        if a["mode"] == "managed":
                            hub.r.stop(aid)
                        else:
                            hub.t.stop(aid, bool(b.get("confirm")))
                    else:
                        hub.switch_account(aid, b.get("account"), bool(b.get("confirm")))
                    return 200, hub.agent_view(hub.s.agent(aid))
                if path == "/api/projects":
                    if not b.get("id"):
                        b["id"] = discovery.slug(b.get("name"))
                    new = not hub.s.project(b["id"])
                    p = hub.s.upsert_project(b)
                    if new:  # a new project gets its own folder under the workspace
                        hub.ensure_project_dir(p["id"])
                        p = hub.s.project(p["id"])
                    return 200, p
                if path == "/api/decisions":
                    if not b.get("question"):
                        raise ValueError("question is required")
                    did = new_id("d")
                    project = b.get("project")
                    if not project and b.get("agent") and hub.s.agent(b["agent"]):
                        project = hub.s.agent(b["agent"])["project"]
                    hub.s.x("INSERT INTO decisions(id,project,agent,question,options,created_at) VALUES(?,?,?,?,?,?)",
                            (did, project, b.get("agent"), b["question"], json.dumps(b.get("options") or []), now()))
                    opts = " / ".join(b.get("options") or [])
                    hub.notify(b.get("agent"), f"asks: {b['question']}" + (f" ({opts})" if opts else ""),
                               kind="decision", idem=f"decision:{did}", project=project)
                    return 200, hub.s.decision(did)
                m = re.fullmatch(r"/api/decisions/([\w.-]+)/answer", path)
                if m:
                    return 200, hub.answer(m.group(1), (b.get("answer") or "").strip())
                if path == "/api/reply":  # observed agent answering a pane pointer: sb reply <turn> "text"
                    turn = b.get("turn")
                    rows = hub.s.q("SELECT * FROM messages WHERE turn_id=?", (turn,))
                    if not rows:
                        raise LookupError(turn)
                    hub.s.x("UPDATE messages SET reply=? WHERE turn_id=?", (b.get("body", "")[:20000], turn))
                    agent = hub.s.agent(rows[0]["to_agent"])
                    hub.r.route_replies(agent, rows, b.get("body", ""))
                    return 200, {"ok": True}
                raise LookupError(path)

            self._guard(route)

        def do_DELETE(self):
            path = urlparse(self.path).path

            def route():
                m = re.fullmatch(r"/api/agents/([\w.-]+)", path)
                if not m or not hub.s.agent(m.group(1)):
                    raise LookupError(path)
                aid = m.group(1)
                if hub.t.running(aid):
                    hub.t.exit_cli(aid, interrupt=True)
                hub.s.set_agent(aid, gone=1, state="stopped")
                hub.s.x("UPDATE projects SET lead=NULL WHERE lead=?", (aid,))
                return 200, {"ok": True}

            self._guard(route)

        def _terminal_ws(self, aid, start):
            a = hub.s.agent(aid)
            key = self.headers.get("Sec-WebSocket-Key")
            if not a or not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
                self.send_response(404 if not a else 400)
                self.end_headers()
                return
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            sock = ws.WebSocket(self.rfile, self.wfile, self.connection)
            hub.terminal_session(sock, a, start)

        def do_PATCH(self):
            path = urlparse(self.path).path

            def route():
                b = self._body()
                m = re.fullmatch(r"/api/projects/([\w.-]+)", path)
                if m:
                    if not hub.s.project(m.group(1)):
                        raise LookupError(m.group(1))
                    allowed = {k: b[k] for k in ("name", "goal", "next_step", "status_note", "sources", "archived", "paused",
                                                 "lead") if k in b}
                    if allowed.get("lead"):
                        la = hub.s.agent(allowed["lead"])
                        if not la or la["project"] != m.group(1):
                            raise ValueError(f"{allowed['lead']} is not an agent on this project")
                    if "lead" in allowed and not allowed["lead"]:
                        allowed["lead"] = ""
                    return 200, hub.s.upsert_project({"id": m.group(1), **allowed})
                m = re.fullmatch(r"/api/agents/([\w.-]+)", path)
                if m:
                    aid = m.group(1)
                    if not hub.s.agent(aid):
                        raise LookupError(aid)
                    if "account" in b:
                        hub.switch_account(aid, b["account"])
                    upd = {}
                    if "project" in b:
                        if b["project"] and b["project"] != "unsorted" and not hub.s.project(b["project"]):
                            raise ValueError(f"unknown project {b['project']}")
                        upd.update(project=b["project"] or "unsorted", project_pinned=1)
                    if "title" in b:
                        upd["title"] = b["title"]
                    if "paused" in b:
                        upd["paused"] = int(bool(b["paused"]))
                    for k in ("model", "effort", "permission_mode"):
                        if k in b:
                            upd[k] = b[k]
                    hub.s.set_agent(aid, **upd)
                    return 200, hub.agent_view(hub.s.agent(aid))
                raise LookupError(path)

            self._guard(route)

        def _static(self, path):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.realpath(os.path.join(WEB, rel))
            if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
                self.send_response(404)
                self.end_headers()
                return
            data = open(full, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

    return H


def main():
    hub = Hub()
    if os.environ.get("SB_SEED_TEST"):  # browser tests: fake CLIs (SB_CLAUDE_BIN, a "test" account), no discovery
        hub.sync_observed = lambda: None
        if not config.configured():  # a throwaway SB_CONFIG: one Claude account for the fake CLI
            config.set_accounts([{"name": "test", "kind": "claude"}])
            hub.ensure_router()
        if os.environ.get("FAKE_PROJECTS"):
            discovery.EXTRA_PROJECTS.append(os.environ["FAKE_PROJECTS"])
        for aid, pid in (("fake-1", "switchboard"), ("fake-2", "switchboard")):
            if not hub.s.agent(aid):
                hub.s.add_agent({"id": aid, "project": pid, "title": aid, "provider": "claude", "account": "test",
                                 "model": "claude-sonnet-5-5" if aid == "fake-1" else "claude-opus-5-5",
                                 "mode": "terminal", "cwd": ROOT, "state": "idle", "project_pinned": 1})
        hub.s.upsert_project({"id": "switchboard", "lead": "fake-1"})
    hub.start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(hub))
    print(f"switchboard on http://127.0.0.1:{PORT}/", flush=True)
    try:
        srv.serve_forever()
    finally:
        hub.stop_flag.set()


if __name__ == "__main__":
    main()
