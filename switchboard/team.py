"""A project's team over a time window: who is on it, and the traffic between them (the Team view).

After dsh-context's Fleet tab (bowenliang123/dsh-context, Apache-2.0): agent cards, a cross-agent timeline with one
lane per agent and an arrow per message, and an event list. Sources here: the messages table (sb send, replies,
owner dispatches), shared notes, work items finished, and decisions filed for the owner.
"""
import time

from .store import iso_from_epoch

TRIM = 1200


def _cut(s):
    s = s or ""
    return s if len(s) <= TRIM else s[:TRIM] + "…"


def view(hub, pid, hours=24.0):
    s = hub.s
    hours = max(1.0, min(float(hours), 24 * 30))
    since = iso_from_epoch(time.time() - hours * 3600)
    mine = {a["id"]: a for a in s.q("SELECT * FROM agents WHERE project=?", (pid,))}
    live = {aid for aid, a in mine.items() if not a.get("gone")}
    ids = tuple(mine) or ("",)
    marks = ",".join("?" * len(ids))
    events = []
    for m in s.q(f"SELECT * FROM messages WHERE created_at >= ? AND (to_agent IN ({marks}) OR from_agent IN ({marks})) "
                 "ORDER BY created_at", (since, *ids, *ids)):
        events.append({"kind": "reply" if m["is_reply"] else "message", "id": m["id"], "t": m["created_at"],
                       "from": m["from_agent"] or "owner", "to": m["to_agent"], "state": m["state"],
                       "delivered": m["delivered_at"], "done": m["finished_at"], "body": _cut(m["body"]),
                       "reply": _cut(m["reply"]), "error": m["error"]})
    for n in s.q("SELECT * FROM notes WHERE project=? AND created_at >= ? ORDER BY id", (pid, since)):
        events.append({"kind": "note", "id": f"n{n['id']}", "t": n["created_at"], "from": n["agent"] or "owner",
                       "body": _cut(n["text"])})
    for w in s.q("SELECT * FROM work WHERE project=? AND status='done' AND done_at >= ?", (pid, since)):
        events.append({"kind": "work", "id": f"w{w['id']}", "t": w["done_at"], "from": w["agent"] or "owner",
                       "body": f"#{w['id']} done: {w['title']}"})
    for d in s.q("SELECT * FROM decisions WHERE project=? AND created_at >= ?", (pid, since)):
        events.append({"kind": "decision", "id": d["id"], "t": d["created_at"], "from": d["agent"] or "?", "to": "owner",
                       "state": d["state"], "done": d["answered_at"], "body": _cut(d["question"]),
                       "reply": _cut(d["answer"])})
    events.sort(key=lambda e: e["t"] or "")
    seen = {e.get("from") for e in events} | {e.get("to") for e in events}
    agents = []
    for aid in sorted(set(live) | (seen - {None, "owner"})):
        a = mine.get(aid) or s.agent(aid) or {"id": aid}
        sent = sum(1 for e in events if e.get("from") == aid and e["kind"] in ("message", "reply"))
        got = sum(1 for e in events if e.get("to") == aid and e["kind"] in ("message", "reply"))
        agents.append({"id": aid, "title": a.get("title") or aid, "provider": a.get("provider"), "model": a.get("model"),
                       "state": a.get("state"), "gone": bool(a.get("gone")), "project": a.get("project"),
                       "outside": a.get("project") != pid, "context_tokens": a.get("context_tokens"),
                       "context_window": a.get("context_window"), "tokens_24h": a.get("tokens_24h"),
                       "last_activity": a.get("last_activity"), "sent": sent, "received": got})
    return {"project": pid, "hours": hours, "since": since, "now": iso_from_epoch(time.time()),
            "lead": hub.lead_of(pid), "agents": agents, "events": events}
