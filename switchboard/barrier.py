"""The project barrier. An agent acts inside its own project; only the owner, by hand, acts across projects.
The one exception is the owner's own project, MAINTAINERS ("switchboard"): its agents act anywhere, as the owner does.

Two questions, answered here so hub.py asks them in one place:

1. What may an agent call? AGENT_ROUTES lists it. A route not listed is the owner's, so a route added later is closed
   until someone opens it on purpose.
2. Who is calling? Every agent runs as the owner's Unix user, so what a caller says about itself (the
   X-Switchboard-Agent header, SB_AGENT) proves nothing: `env -u SB_AGENT sb halt` claims to be nobody, which used to
   mean the owner. identify() looks past the claim at the process on the other end of the socket and its ancestors.

What this cannot stop: the same Unix user can write the HALT file itself, call systemctl, or start a process that looks
like the owner's terminal (`systemd-run --pty`). This barrier stops mistakes and casual workarounds; only a separate
Unix user for agents would make it a wall against an agent working hard to get through.
"""
import os
import re
from collections import namedtuple
from urllib.parse import urlparse

_ID = r"[\w.-]+"
_ROUTES = {
    "GET": rf"""/api/overview /api/fleet /api/work /api/work/\d+ /api/inbox /api/jobs /api/jobs/[\w-]+ /api/helpers
        /api/context /api/projects/{_ID} /api/projects/{_ID}/context /api/memory
        /api/memory/\d+ /api/projects/{_ID}/memory /api/agents
        /api/agents/{_ID} /api/agents/{_ID}/(?:context|chat|image|events|queue) /api/messages /api/messages/{_ID}
        /api/uploads/{_ID}""",
    "POST": rf"""/api/uploads /api/messages /api/messages/{_ID}/(?:cancel|retry) /api/work /api/fleet/probe /api/jobs
        /api/jobs/adopt /api/jobs/[\w-]+/stop /api/agents /api/dispatch /api/notifications /api/notes /api/summon
        /api/helpers /api/decisions /api/inbox/{_ID}/done /api/reply
        /api/agents/{_ID}/(?:interrupt|clear|compact|relaunch|start|stop|account)""",
    "PATCH": rf"/api/work/\d+ /api/projects/{_ID} /api/agents/{_ID}",
    "DELETE": rf"/api/work/\d+ /api/agents/{_ID}",
}
AGENT_ROUTES = {m: [re.compile(r) for r in rs.split()] for m, rs in _ROUTES.items()}
# The owner's own project: Switchboard's agents are the owner's terminal on the whole
# Switchboard. They are not limited like the others: every route, every project's agents, jobs and context.
MAINTAINERS = "switchboard"


def unlimited(project):
    return project == MAINTAINERS

BROWSERS = {"electron", "chrome", "chromium", "firefox", "firefox-bin", "brave", "msedge"}
AGENT_CLIS = {"claude", "codex", "gemini", "hermes", "antigravity"}
OWNER_PROGRAMS = ("switchboard.gate",)  # the paired phone's gate


def agent_may(method, path, project=None):
    if unlimited(project):
        return True
    return any(r.fullmatch(path) for r in AGENT_ROUTES.get(method, ()))


def refusal(path):
    """Written for an agent that just got refused: what it may do instead, and for the owner: where to type it."""
    if path.startswith("/api/fleet/"):
        hint = ("To read the HALT state use `sb fleet`. To stop your own job use `sb stop <id>`. If the whole fleet really "
                "should stop, `sb notify` the owner. ")
    else:
        hint = ""
    return (f"{path} acts across projects, so only the owner can do it and no agent may. {hint}"
            "Owner: use the dashboard, a Switchboard shell pane or your own terminal; a `!` line inside an agent's "
            "session counts as that agent.")


# ---------- who is on the other end of this socket ----------
Caller = namedtuple("Caller", "pid agent unmanaged owner_surface")
NOBODY = Caller(None, None, False, False)


def _inode(client_port, server_port):
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as f:
                next(f, None)
                for line in f:
                    p = line.split()
                    if int(p[1].rsplit(":", 1)[1], 16) == client_port and int(p[2].rsplit(":", 1)[1], 16) == server_port:
                        return p[9]
        except (OSError, ValueError, IndexError):
            continue
    return None


def _holder(inode):
    """The process holding a socket. Newest first: a CLI call is a process that just started."""
    want = f"socket:[{inode}]"
    for pid in sorted((int(d) for d in os.listdir("/proc") if d.isdigit()), reverse=True):
        try:
            with os.scandir(f"/proc/{pid}/fd") as fds:
                for fd in fds:
                    try:
                        if os.readlink(fd.path) == want:
                            return pid
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def _read(pid, name):
    try:
        with open(f"/proc/{pid}/{name}", "rb") as f:
            return f.read()
    except OSError:
        return b""


def _proc(pid):
    stat = _read(pid, "stat").decode("utf-8", "replace")
    if ")" not in stat:
        return None
    rest = stat.rsplit(")", 1)[1].split()  # state ppid pgrp session tty_nr
    env = dict(kv.split("=", 1) for kv in _read(pid, "environ").decode("utf-8", "replace").split("\0") if "=" in kv)
    argv = _read(pid, "cmdline").decode("utf-8", "replace").split("\0")
    return {"pid": pid, "ppid": int(rest[1]), "tty": int(rest[4]), "comm": stat[stat.index("(") + 1:stat.rindex(")")],
            "env": env, "argv": [a for a in argv if a]}


def _ours(env, server_port):
    """Does this process belong to this hub? Without this, a test hub run by an agent of the live hub would take its
    test client for that agent."""
    hub = env.get("SB_HUB")
    return not hub or urlparse(hub).port == server_port


def _is_cli(proc):
    names = {proc["comm"]} | {os.path.basename(a) for a in proc["argv"][:2]}
    return bool(names & AGENT_CLIS)


def _cgroup_is_agents(pid, hub_pid):
    """A process that detached from its parent (setsid, nohup, double fork) stays in the cgroup it started in. Those of
    the hub's cgroup are its agents' terminals; those of an sb-job unit are an agent's job."""
    mine, own = _read(pid, "cgroup"), _read(hub_pid, "cgroup")
    return b"sb-job-" in mine or (b".service" in mine and mine == own)


def identify(client_port, server_port, hub_pid=None):
    """Who holds the client end of this connection. Caller.agent is the agent it descends from (the outermost SB_AGENT:
    that one was set by the hub at launch, the inner ones by the process itself). Caller.unmanaged is an agent CLI or
    an agent's detached child with no agent id to show for it. Caller.owner_surface is true only where a person can be
    at the keyboard: a terminal, the dashboard or the phone gate, with no agent anywhere above it."""
    hub_pid = hub_pid or os.getpid()
    inode = _inode(client_port, server_port)
    pid = _holder(inode) if inode else None
    if not pid:
        return NOBODY
    if pid == hub_pid:  # the hub's own threads
        return Caller(pid, None, False, True)
    agent, unmanaged, program, reached_hub, in_pane = None, False, False, False, False
    first, cur = None, pid
    while cur and cur > 1:
        if cur == hub_pid:
            reached_hub = True
            break
        proc = _proc(cur)
        if not proc:
            break
        first = first or proc
        if _ours(proc["env"], server_port):
            who = proc["env"].get("SB_AGENT")
            if who and who != "owner":
                agent = who
            if _is_cli(proc):
                unmanaged = True
        joined = " ".join(proc["argv"][:4])
        if "switchboard.ptyholder" in joined:
            in_pane = True
        if proc["comm"] in BROWSERS or any(os.path.basename(a) in BROWSERS for a in proc["argv"][:1]) \
                or any(x in joined for x in OWNER_PROGRAMS):
            program = True
        cur = proc["ppid"]
    if not first:
        return NOBODY
    if agent:
        return Caller(pid, agent, False, False)
    if not reached_hub and not in_pane and _ours(first["env"], server_port) and _cgroup_is_agents(pid, hub_pid):
        unmanaged = True
    if unmanaged:
        return Caller(pid, None, True, False)
    return Caller(pid, None, False, bool(program or first["tty"] or reached_hub))


# ---------- what an agent sees of the shared fleet ----------
_HOST_FIELDS = ("name", "ssh", "desktop", "order", "reserve", "labels", "reachable", "error", "probed_at", "age_s",
                "probe_ms", "hostname", "wsl", "user_manager", "uptime_s", "capacity", "desktop_metrics", "errors")


def fleet_for(view, project, agent_ids):
    """The fleet as one project may see it: every host's capacity (it has to place work), but only its own jobs."""
    hosts = []
    for h in view.get("hosts", []):
        tracked = [j for j in h.get("tracked") or [] if j.get("project") == project]
        untracked = [u for u in h.get("untracked") or [] if u.get("agent") in agent_ids]
        units = {j.get("unit") for j in tracked}
        hosts.append({**{k: h[k] for k in _HOST_FIELDS if k in h}, "tracked": tracked, "untracked": untracked,
                      "jobs": [j for j in h.get("jobs") or [] if j.get("unit") in units]})  # a list, as the owner's view has
    placers = dict(view.get("placers") or {})
    if placers.get("workboard"):
        wb = dict(placers["workboard"])
        wb["jobs"] = [j if j.get("project") == project else
                      {"id": j.get("id"), "status": j.get("status"), "lane": j.get("lane"), "host": j.get("host"),
                       "project": "(another project)"} for j in wb.get("jobs") or []]
        wb["preempted"] = [j for j in wb.get("preempted") or [] if j.get("project") == project]
        placers["workboard"] = wb
    config = {k: v for k, v in (view.get("config") or {}).items() if k in ("exists", "jobs", "budgets", "path")}
    queued = [j for j in view.get("queued") or [] if j.get("project") == project]
    return {**view, "hosts": hosts, "queued": queued, "placers": placers, "config": config}
