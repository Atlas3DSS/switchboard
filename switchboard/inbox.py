"""The owner's inbox: every open request an agent has filed for the owner, per project (docs/STATUS-REPORTS.md,
section 2, first slice). Two kinds: a decision (`sb ask "question" --options a,b`) and a need (`sb notify "text"`
without --info). An agent files under its own name and project; only the owner, on the page or through the desk
(`sb inbox`), crosses an item off: by answering (a decision), by acknowledging (a need) or by dismissing (either).
The asking agent is told what the owner did.
"""
import time

from .store import epoch, now


def items(hub, project=None):
    agents = {a["id"]: a for a in hub.s.agents(include_gone=True)}

    def proj_of(row):
        return row.get("project") or (agents.get(row.get("agent")) or {}).get("project") or "unsorted"

    out = []
    for d in hub.s.decisions(open_only=True):
        p = proj_of(d)
        if project and p != project:
            continue
        out.append({"kind": "decision", "id": d["id"], "project": p, "agent": d["agent"], "text": d["question"],
                    "options": d["options"], "created_at": d["created_at"]})
    for n in hub.s.q("SELECT * FROM notifications WHERE acked_at IS NULL AND level='need' ORDER BY created_at DESC"):
        n = dict(n)
        if (n.get("idem") or "").startswith("decision:"):
            continue  # the decision itself is listed above
        p = proj_of(n)
        if project and p != project:
            continue
        out.append({"kind": "need", "id": n["id"], "project": p, "agent": n["agent"], "text": n["text"], "options": [],
                    "created_at": n["created_at"]})
    for x in out:
        t = epoch(x["created_at"])
        x["age_s"] = int(time.time() - t) if t else None
    out.sort(key=lambda x: x["created_at"] or "", reverse=True)
    by_project = {}
    for x in out:
        by_project[x["project"]] = by_project.get(x["project"], 0) + 1
    return {"project": project, "items": out, "count": len(out), "by_project": by_project}


def badge(hub):
    """For /api/overview: how many items wait, per project (cheap)."""
    counts = {}
    for d in hub.s.q("SELECT project, agent FROM decisions WHERE state='open'"):
        counts[d["project"] or "unsorted"] = counts.get(d["project"] or "unsorted", 0) + 1
    for n in hub.s.q("SELECT project, idem FROM notifications WHERE acked_at IS NULL AND level='need'"):
        if (n["idem"] or "").startswith("decision:"):
            continue
        counts[n["project"] or "unsorted"] = counts.get(n["project"] or "unsorted", 0) + 1
    return {"count": sum(counts.values()), "by_project": counts}


def done(hub, iid, answer=None, by="owner"):
    """Cross one item off. A decision with an answer is answered; without one it is dismissed. A need is acked."""
    d = hub.s.decision(iid)
    if d:
        if d["state"] != "open":
            return {"kind": "decision", "id": iid, "state": d["state"], "answer": d.get("answer")}
        if answer:
            hub.answer(iid, answer)
            return {"kind": "decision", "id": iid, "state": "answered", "answer": answer}
        hub.s.x("UPDATE decisions SET state='dismissed', answer=?, answered_at=? WHERE id=?",
                (f"dismissed by {by}", now(), iid))
        hub.ack(where="idem=?", args=(f"decision:{iid}",))
        if d["agent"] and hub.s.agent(d["agent"]):
            hub.s.enqueue(d["agent"], f"Owner closed your question {iid} without answering it (\"{d['question'][:200]}\"): "
                                      "treat it as no longer wanted; ask again only if it still matters.",
                          frm="owner", idem=f"dismiss:{iid}")
        return {"kind": "decision", "id": iid, "state": "dismissed"}
    n = hub.s.one("SELECT * FROM notifications WHERE id=?", (iid,))
    if not n:
        raise LookupError(iid)
    hub.ack(iid)
    return {"kind": "need", "id": iid, "state": "acked"}
