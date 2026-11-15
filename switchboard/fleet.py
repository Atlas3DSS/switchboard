"""Fleet controls for the Switchboard chat: one message to every active agent (broadcast, wrap-up), an executive
brief of what active and recently active agents are doing, and the owner's master work queue.

A broadcast is one ordinary message per agent (exactly-once, typed when that agent's composer is empty and idle),
grouped under a broadcast id so the replies can be read together. Only the owner, or the Switchboard chat acting
for them, may broadcast; an agent reaches other agents one at a time with `sb send`.
"""
import json
import threading
import time

from . import discovery, shared
from .store import epoch, new_id, now

WORK_STATUS = ("todo", "doing", "blocked", "done", "dropped")
WORK_OPEN = ("todo", "doing", "blocked")
STALE_DAYS = 2  # an open item untouched this long is put to its agent
NUDGE_EVERY_S = 86400  # at most one work-queue check per agent per day
NUDGE = ("Switchboard work-queue check: these open items are yours and have not been touched for {days}+ days:\n{items}\n"
         "For each one, right now: `sb work done <n>` if it is finished, `sb work drop <n> --note \"why\"` if it is no "
         "longer wanted, otherwise `sb work set <n> --note \"one line on where it stands\"`. Then carry on with what "
         "you were doing; no reply needed.")
FINAL = ("done", "failed", "interrupted")
BROADCAST_MAX_S = 86400  # a broadcast nobody finishes answering is closed after a day

WRAPUP = (
    "Wrap up for now. Bring your work to a safe stopping point: finish or cleanly pause the step you are on and "
    "start nothing new. Leave long-running jobs running in systemd units only on purpose (say which), stop the rest. "
    "Commit or stash work in progress with a clear message, and write what the next session needs into the "
    "project's handoff notes (`sb note \"...\"` for a one-liner). Put everything unfinished on the master work "
    "queue with `sb work add \"...\"` and mark what you finished with `sb work done <n>` (`sb work` lists it). "
    "Then end your turn with exactly this, short:\n"
    "STATUS: stopped | still running (what, where)\n"
    "DONE: ...\n"
    "LEFT: ...\n"
    "NEEDS OWNER: ... (or none)")


def _ago(iso):
    t = epoch(iso)
    if not t:
        return "never"
    s = max(0, time.time() - t)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "just now"


def _clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + " …"


# ---------- broadcast ----------
def active_agents(hub, project=None, only=None):
    """Who a broadcast reaches: every live Claude/Codex terminal, tmux pane session or headless turn. Never the
    Switchboard chat, a paused agent or a plain shell; a stopped terminal is not woken up."""
    turns = {r["to_agent"] for r in hub.r.running_turns()}
    out = []
    for a in hub.s.agents():
        if a.get("role") == "desk" or a["provider"] not in discovery.AGENT_CLIS or a["paused"]:
            continue
        if project and a["project"] != project:
            continue
        if only is not None:
            if a["id"] in only:
                out.append(a)
        elif hub.t.running(a["id"]) or a["id"] in turns or (a["mode"] == "observed" and a.get("pane")):
            out.append(a)
    return out


def broadcast(hub, b):
    frm = b.get("from") or "owner"
    if frm != "owner":
        raise PermissionError("only the owner messages every agent at once; "
                              "use sb send <agent>")
    kind = "wrapup" if b.get("wrapup") else "message"
    text = (b.get("body") or "").strip()
    if kind == "message" and not text:
        raise ValueError("body is required")
    if b.get("idem"):
        row = hub.s.one("SELECT * FROM broadcasts WHERE idem=?", (b["idem"],))
        if row:
            return {**broadcast_view(hub, row), "duplicate": True}
    project = b.get("project") or None
    if project and not hub.s.project(project):
        raise ValueError(f"unknown project {project}")
    only = b.get("agents") or None
    targets = active_agents(hub, project, set(only) if only else None)
    if only:
        missing = set(only) - {a["id"] for a in targets}
        if missing:
            raise ValueError(f"not a Claude/Codex agent that can take messages: {', '.join(sorted(missing))}")
    if not targets:
        raise ValueError("no active agents" + (f" on project {project}" if project else "") +
                         " (only running terminals are reached; stopped ones are not woken up)")
    body = (WRAPUP + (f"\n\nAlso from the owner: {text}" if text else "")) if kind == "wrapup" else text
    who = f"all {len(targets)} active agents" + (f" on {project}" if project else "")
    body = f"[Owner to {who}, via Switchboard{' (wrap-up)' if kind == 'wrapup' else ''}]\n{body}"
    bid = new_id("b")
    sent = []
    with hub.s.tx():  # the ledger row and every message, or none of them
        row = hub.s.one("SELECT * FROM broadcasts WHERE idem=?", (b["idem"],)) if b.get("idem") else None
        if not row:
            for a in targets:
                m, _ = hub.s.enqueue(a["id"], body, frm="owner", idem=f"{bid}:{a['id']}", hop=1)
                sent.append({"agent": a["id"], "message": m["id"]})
            hub.s.x("INSERT INTO broadcasts(id,idem,kind,body,from_agent,project,close,messages,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (bid, b.get("idem") or bid, kind, text, frm, project,
                                                 int(bool(b.get("close"))), json.dumps(sent), now()))
    if row:
        return {**broadcast_view(hub, row), "duplicate": True}
    for a in targets:
        if b.get("interrupt") and hub.t.running(a["id"]) and hub.t.term(a["id"]).view()[1]:
            hub.t.term(a["id"]).write("\x1b")  # Esc: stop the current step so the message lands now
            hub.s.event(a["id"], "system", f"interrupted by the owner's broadcast {bid}")
    return {**broadcast_view(hub, hub.s.one("SELECT * FROM broadcasts WHERE id=?", (bid,))), "duplicate": False}


def broadcast_view(hub, row, clip=None):
    closed = set(json.loads(row["closed"] or "[]"))
    out = []
    for r in json.loads(row["messages"] or "[]"):
        m = hub.s.message(r["message"]) or {}
        reply = m.get("reply") or ""
        out.append({"agent": r["agent"], "message": r["message"], "state": m.get("state", "failed"),
                    "reply": _clip(reply, clip) if clip else reply, "error": m.get("error"),
                    "closed": r["agent"] in closed})
    return {"id": row["id"], "kind": row["kind"], "body": row["body"], "from": row["from_agent"],
            "project": row["project"], "close": bool(row["close"]), "created_at": row["created_at"],
            "finished_at": row["finished_at"], "agents": out,
            "replied": sum(1 for x in out if x["state"] == "done"), "total": len(out)}


def broadcasts(hub, hours=24, clip=None):
    since = time.time() - hours * 3600
    rows = hub.s.q("SELECT * FROM broadcasts ORDER BY created_at DESC LIMIT 20")
    return [broadcast_view(hub, r, clip) for r in rows if epoch(r["created_at"]) >= since or not r["finished_at"]]


def tick(hub):
    """Close terminals that asked to be closed once they have answered; finish broadcasts and tell the owner."""
    for row in hub.s.q("SELECT * FROM broadcasts WHERE finished_at IS NULL"):
        v = broadcast_view(hub, row)
        closed = set(json.loads(row["closed"] or "[]"))
        for x in v["agents"]:
            aid = x["agent"]
            if row["close"] and x["state"] == "done" and aid not in closed and hub.t.running(aid) \
                    and not hub.t.busy(aid):
                closed.add(aid)
                hub.s.event(aid, "system", f"closed after answering the owner's wrap-up {row['id']}")
                threading.Thread(target=hub.t.exit_cli, args=(aid,), daemon=True).start()
        if closed != set(json.loads(row["closed"] or "[]")):
            hub.s.x("UPDATE broadcasts SET closed=? WHERE id=?", (json.dumps(sorted(closed)), row["id"]))
        if all(x["state"] in FINAL for x in v["agents"]) or time.time() - epoch(row["created_at"]) > BROADCAST_MAX_S:
            hub.s.x("UPDATE broadcasts SET finished_at=? WHERE id=?", (now(), row["id"]))
            bad = [x["agent"] for x in v["agents"] if x["state"] != "done"]
            what = "Wrap-up" if row["kind"] == "wrapup" else "Broadcast"
            hub.notify(None, f"{what}: {v['replied']}/{v['total']} agents answered"
                       + (f" (no answer: {', '.join(bad)})" if bad else "") + ".",
                       kind="broadcast", level="info", idem=f"broadcast:{row['id']}")


# ---------- brief ----------
def brief(hub, hours=24):
    """Agents running now or active in the last `hours`, with what each was last asked and said, plus broadcast
    replies, the work queue, open decisions and what is waiting on the owner. `text` is what `sb brief` prints."""
    since = time.time() - hours * 3600
    agents = []
    for a in hub.s.agents():
        if a.get("role") == "desk" or a["provider"] not in discovery.AGENT_CLIS:
            continue
        st = hub.t.status(a)
        live = st["running"] or (a["mode"] == "observed" and a.get("pane"))
        last = max(epoch(a["last_activity"]), epoch(a.get("last_reply_at")))
        if not live and last < since:
            continue
        asked, said = shared.last_exchange(hub.transcript_of(a), a["provider"], asked_max=300, said_max=900)
        agents.append({"id": a["id"], "title": a["title"], "project": a["project"], "model": a["model"],
                       "account": a["account"], "state": st["state"] if st["running"] else a["state"],
                       "running": bool(live), "last_activity": a["last_activity"], "_last": last,
                       "waiting": hub.t.waiting.get(a["id"]), "asked": asked, "said": said,
                       "queued": hub.s.one("SELECT COUNT(*) n FROM messages WHERE to_agent=? AND state='queued'",
                                           (a["id"],))["n"]})
    agents.sort(key=lambda x: -x.pop("_last"))
    pids = []
    for x in agents:
        if x["project"] not in pids:
            pids.append(x["project"])
    projects = []
    for pid in pids:
        p = hub.s.project(pid) or {"id": pid, "name": pid, "next_step": ""}
        projects.append({"id": pid, "name": p["name"], "next_step": p.get("next_step") or "",
                         "agents": [x for x in agents if x["project"] == pid]})
    work = work_list(hub, status="open")["items"]
    done = [w for w in work_list(hub, status="all")["items"]
            if w["status"] == "done" and epoch(w["done_at"]) >= since]
    failed = hub.s.q("SELECT id,to_agent,error,created_at FROM messages WHERE state IN ('failed','held') "
                     "AND created_at>=? ORDER BY created_at DESC LIMIT 10",
                     (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),))
    out = {"ts": now(), "hours": hours, "projects": projects, "broadcasts": broadcasts(hub, hours, clip=900),
           "work_open": work, "work_done": done, "decisions": hub.s.decisions(open_only=True),
           "needs": [n for n in hub.notifications()["items"] if n["level"] == "need"], "failed": failed}
    out["text"] = brief_text(out)
    return out


def brief_text(b):
    L = [f"BRIEF {b['ts'][:16].replace('T', ' ')} UTC: agents running now or active in the last {b['hours']:g} h, "
         "busiest projects first"]
    if not b["projects"]:
        L.append("(no agent was active in this window)")
    for p in b["projects"]:
        L.append(f"\nPROJECT {p['id']} - {p['name']}" + (f" (next step: {_clip(p['next_step'], 140)})"
                                                            if p["next_step"] else ""))
        for a in p["agents"]:
            st = ("working" if a["state"] == "busy" else a["state"]) if a["running"] else "not running"
            L.append(f"  * {a['id']} [{st}, {a['model'] or 'default model'}, active {_ago(a['last_activity'])}"
                     + (f", {a['queued']} message(s) queued" if a["queued"] else "") + "]")
            if a["waiting"]:
                L.append(f"    WAITING FOR THE OWNER IN ITS TERMINAL: {a['waiting']}")
            L.append(f"    asked: {a['asked'] or '(nothing yet)'}")
            L.append(f"    said:  {a['said'] or '(nothing yet)'}")
    for x in b["broadcasts"]:
        what = "WRAP-UP" if x["kind"] == "wrapup" else "BROADCAST"
        L.append(f"\n{what} {x['id']} ({_ago(x['created_at'])}, {x['replied']}/{x['total']} answered"
                 + (", closing terminals after they answer" if x["close"] else "")
                 + (f"): {_clip(x['body'], 160)}" if x["body"] else ")"))
        for a in x["agents"]:
            if a["state"] == "done":
                L.append(f"  - {a['agent']}{' (closed)' if a['closed'] else ''}: {a['reply'] or '(empty reply)'}")
            else:
                L.append(f"  - {a['agent']}: no answer yet ({a['state']}{': ' + a['error'] if a['error'] else ''})")
    L.append(f"\nWORK QUEUE ({len(b['work_open'])} open)")
    for w in b["work_open"]:
        L.append("  " + work_line(w))
    if b["work_done"]:
        L.append(f"  done in the window: " + "; ".join(f"#{w['id']} {w['title']}" for w in b["work_done"]))
    if b["decisions"]:
        L.append("\nOPEN DECISIONS FOR THE OWNER")
        for d in b["decisions"]:
            L.append(f"  - {d['id']} ({d['project'] or '-'}, from {d['agent'] or '-'}): {_clip(d['question'], 200)}")
    if b["needs"]:
        L.append("\nIN THE OWNER'S INBOX")
        for n in b["needs"]:
            L.append(f"  - {n['agent'] or '-'}: {_clip(n['text'], 200)}")
    if b["failed"]:
        L.append("\nMESSAGES NOT DELIVERED")
        for m in b["failed"]:
            L.append(f"  - {m['id']} to {m['to_agent']}: {_clip(m['error'], 160)}")
    return "\n".join(L)


# ---------- master work queue ----------
def _work_row(r):
    return {**r, "open": r["status"] in WORK_OPEN}


def work_line(w):
    who = ", ".join(x for x in (w["project"], w["agent"] and "agent " + w["agent"]) if x)
    return f"#{w['id']} [P{w['priority']} {w['status']}] {w['title']}" + (f" ({who})" if who else "") + \
        (f" - {_clip(w['detail'], 200)}" if w["detail"] else "")


def work_list(hub, project=None, status="open", agent=None):
    sql, args = "SELECT * FROM work WHERE 1=1", []
    if project:
        sql += " AND project=?"
        args.append(project)
    if agent:
        sql += " AND agent=?"
        args.append(agent)
    if status == "open":
        sql += f" AND status IN ({','.join('?' * len(WORK_OPEN))})"
        args += WORK_OPEN
    elif status in WORK_STATUS:
        sql += " AND status=?"
        args.append(status)
    elif status != "all":
        raise ValueError(f"status is open, all or one of {', '.join(WORK_STATUS)}")
    sql += (" ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'todo' THEN 1 WHEN 'blocked' THEN 2 WHEN 'done' THEN 3 "
            "ELSE 4 END, priority, CASE WHEN status IN ('done','dropped') THEN -id ELSE id END LIMIT 500")
    items = [_work_row(r) for r in hub.s.q(sql, args)]
    counts = {r["status"]: r["n"] for r in hub.s.q("SELECT status, COUNT(*) n FROM work GROUP BY status")}
    return {"items": items, "counts": counts}


def _check(hub, b):
    out = {}
    if "title" in b:
        out["title"] = " ".join((b["title"] or "").split())
        if not out["title"]:
            raise ValueError("title is required")
        if len(out["title"]) > 300:
            raise ValueError("a title is at most 300 characters; put the rest in detail")
    if "detail" in b:
        out["detail"] = (b["detail"] or "").strip()[:4000]
    if "project" in b:
        out["project"] = b["project"] or None
        if out["project"] and not hub.s.project(out["project"]):
            raise ValueError(f"unknown project {out['project']}")
    if "agent" in b:
        out["agent"] = b["agent"] or None
        if out["agent"] and not hub.s.agent(out["agent"]):
            raise ValueError(f"unknown agent {out['agent']}")
    if "priority" in b and b["priority"] is not None:
        try:
            out["priority"] = int(b["priority"])
        except (TypeError, ValueError):
            raise ValueError("priority is 1 (urgent), 2 or 3")
        if out["priority"] not in (1, 2, 3):
            raise ValueError("priority is 1 (urgent), 2 or 3")
    if "status" in b and b["status"] is not None:
        if b["status"] not in WORK_STATUS:
            raise ValueError(f"status is one of {', '.join(WORK_STATUS)}")
        out["status"] = b["status"]
    return out


def work_item(hub, wid):
    r = hub.s.one("SELECT * FROM work WHERE id=?", (int(wid),))
    if not r:
        raise LookupError(f"work item #{wid}")
    return _work_row(r)


def work_add(hub, b):
    frm = b.get("from") or "owner"
    w = _check(hub, {"title": b.get("title"), **{k: b[k] for k in ("detail", "project", "agent", "priority", "status")
                                                  if k in b}})
    if "project" not in b:  # an agent's item belongs to its own project
        me = hub.s.agent(frm) if frm != "owner" else None
        w["project"] = me["project"] if me and me.get("role") != "desk" else None
    if w.get("status") in ("done", "dropped"):
        raise ValueError("a new item is todo, doing or blocked")
    t = now()
    cur = hub.s.x("INSERT INTO work(project,title,detail,status,priority,agent,created_by,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (w.get("project"), w["title"], w.get("detail") or "",
                                                w.get("status") or "todo", w.get("priority") or 2, w.get("agent"),
                                                frm, t, t))
    return work_item(hub, cur.lastrowid)


def work_update(hub, wid, b):
    cur = work_item(hub, wid)
    upd = _check(hub, b)
    if b.get("note"):
        line = f"[{now()[:16].replace('T', ' ')} {b.get('from') or 'owner'}] {' '.join(b['note'].split())}"
        upd["detail"] = ((upd.get("detail", cur["detail"]) or "") + "\n" + line).strip()[-4000:]
    if not upd:
        return cur
    if "status" in upd and upd["status"] != cur["status"]:
        upd["done_at"] = now() if upd["status"] in ("done", "dropped") else None
    upd["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in upd)
    hub.s.x(f"UPDATE work SET {cols} WHERE id=?", (*upd.values(), cur["id"]))
    return work_item(hub, cur["id"])


def work_nudge(hub, running=None):
    """The queue keeps itself current: an agent whose open items have sat untouched for STALE_DAYS is asked, once a
    day, to close, drop or annotate them (the owner should not be the one clicking Done). Only a running agent is
    asked, about its own items and, when it leads its project, the project's unowned ones. Returns the agents asked."""
    running = running or hub.t.running
    nudged = hub.s.setting("work_nudged", {}) or {}
    cutoff = time.time() - STALE_DAYS * 86400
    asked = []
    for a in hub.s.agents():
        aid = a["id"]
        if a["provider"] not in discovery.AGENT_CLIS or a.get("role") == "helper" or not running(aid):
            continue
        if time.time() - (nudged.get(aid) or 0) < NUDGE_EVERY_S:
            continue
        lead = a.get("project") and hub.lead_of(a["project"]) == aid
        items = [w for w in work_list(hub, agent=aid)["items"] if epoch(w["updated_at"]) < cutoff]
        if lead:
            items += [w for w in work_list(hub, project=a["project"])["items"]
                      if not w["agent"] and epoch(w["updated_at"]) < cutoff]
        if not items:
            continue
        lines = "\n".join(f"#{w['id']} [{w['status']}, {int((time.time() - epoch(w['updated_at'])) // 86400)}d] "
                          f"{_clip(w['title'], 160)}" for w in sorted(items, key=lambda w: w["id"])[:15])
        ids = ",".join(str(w["id"]) for w in sorted(items, key=lambda w: w["id"])[:15])
        hub.s.enqueue(aid, NUDGE.format(days=STALE_DAYS, items=lines), frm="owner",
                      idem=f"work-nudge:{aid}:{int(time.time() // NUDGE_EVERY_S)}:{ids}")
        nudged[aid] = time.time()
        asked.append(aid)
    if asked:
        hub.s.set_setting("work_nudged", {k: v for k, v in nudged.items() if time.time() - v < 7 * 86400})
    return asked


def work_delete(hub, wid):
    cur = work_item(hub, wid)
    hub.s.x("DELETE FROM work WHERE id=?", (cur["id"],))
    return {"ok": True, "id": cur["id"]}
