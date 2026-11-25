"""Ghost agents: a panel sealed off inside its own project.

A ghost knows only the task it is given in its own conversation. It is created with ghost=1 (the Ghost box in New agent;
POST /api/agents {ghost: true}), and from then on:

  what it cannot reach   the project's shared context, notes, running memory, outlines and work queue, the other
                         agents (no sb context / memory / note / send / work / helper), and the project's
                         auto-memory folder: it gets an empty private one. may() is its whole route list, checked
                         before the project barrier, so a ghost on the owner's own project is sealed too.
  who can see it         the owner. To every agent, of its own project or any other, it does not exist: not in
                         sb context, sb agents, the overview, a search, a seat, nor reachable by sb send. The one
                         exception is the owner's own project (barrier.MAINTAINERS), which starts, messages and
                         removes a ghost when the owner asks; even there it is in no shared context, search or memory.
  what it leaves         its edits to files. Nothing of it is written to the project's memory while it runs, and
                         when it is removed close() erases the rest: its conversations and the CLI's state for
                         them, its private memory folder, terminal scrollback, and every row the hub kept about it.

What this cannot stop: every agent runs as the owner's Unix user, so a ghost that goes looking can still read files
on disk (another agent's transcript, the project's memory folder), and so can others read its transcript while it
is up. The seal is on what Switchboard hands out, like the project barrier.
"""
import os
import re

from .store import STATE

PROVIDERS = ("claude", "codex", "antigravity")  # Hermes keeps its sessions in its own database: nothing to erase by
HOME = os.path.join(STATE, "ghost")
_ID = r"[\w.-]+"
_ROUTES = {
    "GET": rf"/api/fleet /api/uploads/{_ID}",
    "POST": r"/api/notifications /api/decisions /api/uploads /api/reply",
}
ROUTES = {m: [re.compile(r) for r in rs.split()] for m, rs in _ROUTES.items()}


def may(method, path, me):
    """What a ghost may call: page the owner, ask the owner, attach a file, read the fleet, and read itself."""
    if method == "GET" and path in (f"/api/agents/{me['id']}", f"/api/agents/{me['id']}/queue"):
        return True
    return any(r.fullmatch(path) for r in ROUTES.get(method, ()))


REFUSAL = ("you are a ghost agent: the project's context, memory, notes, work queue and other agents are closed to "
           "you, to read and to write. Work from the task you were given; `sb notify` and `sb ask` reach the owner.")


def ids(store):
    return {r["id"] for r in store.q("SELECT id FROM agents WHERE ghost=1")}


def memdir(agent):
    return os.path.join(HOME, agent["id"], "memory")


def note(agent):
    """What a ghost is told at launch, in place of shared.launch_note."""
    return (f"You are agent '{agent['id']}', a ghost agent in the owner's Switchboard. You are sealed off: you know "
            "only the task the owner gives you in this conversation, and you work from that and from the files in "
            "front of you. The project's shared context, notes, running memory, work queue and other agents are "
            "closed to you, to read and to write: `sb context`, `sb memory`, `sb note`, `sb send`, `sb work`, "
            "and `sb helper` are refused, and no other agent can see or reach you. Do not look for another "
            "way to that information (other agents' transcripts, memory folders). Nothing you learn here is kept: "
            "when the owner removes you, this conversation and every record of it is erased, and only your edits to "
            "files remain. So leave what matters in the files themselves, and do not write memory files or notes "
            "about this work. `sb notify \"text\"` pages the owner and `sb ask \"question\" --options a,b` asks the "
            "owner a question; `sb fleet` shows the machines. Run heavy work in your own shell.")


def close(hub, aid):
    """Remove a ghost and every record of it: only its edits to files stay."""
    import shutil
    from . import kill, rewind
    s = hub.s
    a = s.agent(aid)
    if not a or not a.get("ghost"):
        raise LookupError(aid)
    try:
        if hub.t.running(aid):
            hub.t.exit_cli(aid, interrupt=True)
    except Exception:
        pass
    sids = rewind.sessions(hub, a)
    paths = [p for p in (rewind._forget(a, sid) for sid in sids) if p]
    kill._unindex(hub, paths)
    if a["provider"] == "claude":
        for cfg in kill._claude_config_dirs():
            for sid in sids:
                rewind._rm(os.path.join(cfg, "jobs", sid[:8]))
    shutil.rmtree(os.path.join(HOME, aid), ignore_errors=True)
    rewind._rm(os.path.join(rewind.REWINDS, aid))
    for q in hub.t.paths(aid).values():
        rewind._rm(q)
    dead = [r["id"] for r in s.q("SELECT id FROM memory_sessions WHERE agent=?", (aid,))]
    if dead:
        s.x("DELETE FROM memory_sessions WHERE agent=?", (aid,))
        rewind.scrub_outlines(s, a["project"], dead)
    s.x("DELETE FROM messages WHERE to_agent=? OR from_agent=?", (aid, aid))
    s.x("DELETE FROM events WHERE agent=?", (aid,))
    for table in ("notes", "notifications", "decisions"):
        s.x(f"DELETE FROM {table} WHERE agent=?", (aid,))
    s.x("DELETE FROM settings WHERE key=?", (f"autoclear:{aid}",))
    s.x("UPDATE projects SET lead=NULL WHERE lead=?", (aid,))
    s.x("DELETE FROM agents WHERE id=?", (aid,))
    return {"ok": True, "ghost": True, "sessions": len(paths)}
