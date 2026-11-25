"""Kill a project entirely (owner only): its agents, their conversations and everything the hub recorded about it.

What goes: every agent of the project (running CLIs are exited), their terminal scrollback and rewind copies, the
messages, events, notes, questions, pages, work items, broadcasts, memory and outlines, the seats, the project row
itself. Its jobs that are still running are stopped. Its sessions are deleted from disk, with Claude's per-session
state: the transcripts of its agents, of its remembered past sessions and of every indexed session started in its
folder. No copy is kept anywhere: a killed project leaves only the work.

What stays: the project's files on disk and units that were only adopted for watching (they keep running, untracked).
A killed project is remembered in the `killed_projects` setting so projects.json does not seed it back at the next
start; creating a project with the same id again clears that.
"""
import os

from . import discovery, jobs, rewind
from .terminals import memory_dir

SETTING = "killed_projects"
PROTECTED = ("switchboard", "unsorted")


def killed(hub):
    return list(hub.s.setting(SETTING, []) or [])


def revive(hub, pid):
    """A project created again under a killed id is a new project: the seed file may know it again."""
    left = [p for p in killed(hub) if p != pid]
    if len(left) != len(killed(hub)):
        hub.s.set_setting(SETTING, left)


_MEMDIRS = {}


def _memdir(agent):
    """The agent's auto-memory folder, or None when it cannot be told (no folder, an unknown account)."""
    k = (agent.get("account"), agent.get("cwd"))
    if k not in _MEMDIRS:
        try:
            d = memory_dir(agent) if agent.get("cwd") else None
        except Exception:
            d = None
        _MEMDIRS[k] = d if d and os.path.basename(d) == "memory" else None
    return _MEMDIRS[k]


def _claude_config_dirs():
    out = {os.path.expanduser("~/.claude")}
    try:
        for a in discovery.accounts():
            if a["kind"] == "claude":
                out.add(discovery.account_env(a["name"]).get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))
    except Exception:
        pass
    return sorted(out)


def _transcripts(hub, pid, agents):
    """Every transcript that is this project's: {path: (provider, session id)}."""
    out = {}
    for a in agents:
        if a.get("session_id") and a.get("provider") in ("claude", "codex"):
            p = discovery.transcript_path(a["provider"], a["session_id"], a.get("account"))
            if p:
                out[p] = (a["provider"], a["session_id"])
    for r in hub.s.q("SELECT provider, session_id, path FROM memory_sessions WHERE project=?", (pid,)):
        if r["path"]:
            out.setdefault(r["path"], (r["provider"], r["session_id"]))
    # sessions started in the project's folder that never became an agent here, unless another project's agent has them
    others = {a["session_id"] for a in hub.s.agents(include_gone=True) if a["project"] != pid and a.get("session_id")}
    ix = getattr(hub, "_ix", None)
    if ix is not None:
        try:
            with ix.lock:
                rows = [dict(r) for r in ix.db.execute("SELECT id, provider, path, cwd FROM sess")]
        except Exception:
            rows = []
        for r in rows:
            if r["path"] and r["id"] not in others and hub.project_for(r["cwd"]) == pid:
                out.setdefault(r["path"], (r["provider"], r["id"]))
    return out


def _unindex(hub, paths):
    ix = getattr(hub, "_ix", None)
    if ix is None or not paths:
        return
    try:
        with ix.lock, ix.db:
            for p in paths:
                r = ix.db.execute("SELECT id FROM sess WHERE path=?", (p,)).fetchone()
                ix.db.execute("DELETE FROM sess WHERE path=?", (p,))
                if r:
                    ix.db.execute("DELETE FROM sess_fts WHERE id=?", (r["id"],))
    except Exception:
        pass  # the index notices a vanished file on its own


def kill_project(hub, pid):
    s = hub.s
    p = s.project(pid)
    if not p:
        raise LookupError(pid)
    if pid in PROTECTED:
        raise PermissionError(f"{pid} cannot be killed")
    agents = [a for a in s.agents(include_gone=True) if a["project"] == pid]
    aids = [a["id"] for a in agents]

    # 1. nothing of it keeps running: its jobs, then its CLIs
    stopped_jobs, left_running = [], []
    for j in s.q("SELECT id, name, state, adopted FROM jobs WHERE project=? AND state IN ('queued','running','stopping')",
                 (pid,)):
        if j["adopted"]:
            left_running.append(j["name"])  # only ever watched: the unit is not the hub's to stop
            continue
        try:
            jobs.stop(hub, j["id"], "owner", f"project {pid} killed")
            stopped_jobs.append(j["name"])
        except Exception as e:  # an unreachable host: the row stays so the job is not lost from view
            left_running.append(f"{j['name']} ({e})")
    for aid in aids:
        try:
            if hub.t.running(aid):
                hub.t.exit_cli(aid, interrupt=True)
        except Exception:
            pass

    # 2. its sessions are deleted, with Claude's own state for them; no copy is kept
    files = _transcripts(hub, pid, agents)
    deleted = 0
    for path, (provider, sid) in files.items():
        if provider == "claude" and sid:
            for q in rewind._session_files(path, sid):
                rewind._rm(q)
        if os.path.lexists(path):
            rewind._rm(path)
            deleted += not os.path.lexists(path)
    _unindex(hub, list(files))
    # Claude's background-job records for those sessions: left behind, `claude agents` keeps listing the session and
    # the hub observes it again as an agent of whichever project matches its folder
    sids = {sid for _, sid in files.values() if sid} | {a["session_id"] for a in agents if a.get("session_id")}
    for cfg in _claude_config_dirs():
        for sid in sids:
            rewind._rm(os.path.join(cfg, "jobs", sid[:8]))
    # the agents' auto-memory folders: memories never bleed from a killed project into another.
    # A folder a living agent of another project also writes to is not this project's alone: it stays, and is named.
    theirs = {_memdir(a) for a in s.agents() if a["project"] != pid and a["provider"] == "claude"}
    memory_shared = []
    for d in sorted({_memdir(a) for a in agents if a["provider"] == "claude"} - {None}):
        if d in theirs:
            memory_shared.append(d)
        else:
            rewind._rm(d)
    for aid in aids:
        rewind._rm(os.path.join(rewind.REWINDS, aid))
        for q in hub.t.paths(aid).values():
            rewind._rm(q)

    # 3. the hub's record of it
    if aids:
        marks = ",".join("?" * len(aids))
        s.x(f"DELETE FROM messages WHERE to_agent IN ({marks}) OR from_agent IN ({marks})", (*aids, *aids))
        s.x(f"DELETE FROM events WHERE agent IN ({marks})", aids)
        s.x(f"DELETE FROM settings WHERE key IN ({marks})", [f"autoclear:{a}" for a in aids])
    for table in ("notes", "decisions", "notifications", "work", "broadcasts", "memory", "memory_sessions",
                  "memory_versions"):
        s.x(f"DELETE FROM {table} WHERE project=?", (pid,))
    s.x("DELETE FROM jobs WHERE project=? AND state NOT IN ('queued','running','stopping')", (pid,))
    s.x("DELETE FROM settings WHERE key=?", (f"tab_order:{pid}",))
    seats = s.setting("nodes", {}) or {}
    if pid in seats:
        seats.pop(pid)
        s.set_setting("nodes", seats)
    s.x("DELETE FROM agents WHERE project=?", (pid,))
    s.x("DELETE FROM projects WHERE id=?", (pid,))
    s.set_setting(SETTING, sorted(set(killed(hub)) | {pid}))
    return {"ok": True, "project": pid, "agents": len(aids), "sessions": deleted, "jobs_stopped": stopped_jobs,
            "left_running": left_running, "memory_shared": memory_shared}
