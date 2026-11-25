"""The owner's inbox: every open request an agent has filed for the owner, per project (docs/STATUS-REPORTS.md,
section 2, first slice). Two kinds: a decision (`sb ask "question" --options a,b`) and a need (`sb notify "text"`
without --info). A third comes from the hub itself: a terminal sitting on a choice only the owner can make (a
permission or trust dialog) is listed with the dialog's live options, and picking one answers it in the terminal.
An agent files under its own name and project; only the owner, on the page or through the desk (`sb inbox`),
crosses an item off: by answering (a decision or a terminal choice), by acknowledging or replying (a need) or by
dismissing (either). The asking agent is told what the owner did; a terminal choice is simply made.
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
        item = {"kind": "need", "id": n["id"], "project": p, "agent": n["agent"], "text": n["text"], "options": [],
                "created_at": n["created_at"], "source": n.get("source")}
        if n.get("kind") == "waiting":  # the hub noticed a dialog: offer its options right here
            ch = live_choices(hub, n["agent"])
            if ch:
                item.update(options=ch["options"], choices=True,
                            text=f"{n['text']}\n{ch['question']}" if ch["question"] else n["text"])
        out.append(item)
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


def live_choices(hub, aid):
    t = hub.t.term(aid) if aid else None
    try:
        return t.choices() if t else None
    except (OSError, ValueError):
        return None


def _pick(answer, options):
    """Which option the owner means: its label (case does not matter), its 1-based number, or a unique prefix."""
    a = (answer or "").strip().lower()
    for i, o in enumerate(options):
        if a == o.lower() or a == str(i + 1):
            return i
    hits = [i for i, o in enumerate(options) if o.lower().startswith(a)]
    return hits[0] if len(hits) == 1 else None


def done(hub, iid, answer=None, by="owner", quiet=False):
    """Cross one item off. A decision with an answer is answered; without one it is dismissed. A terminal choice with
    an answer is made in the terminal. A need is acked, or replied to when an answer comes with it; the agent that
    filed it hears either (an ack only while it runs: nothing is started just to be told). quiet: the item just goes,
    the agent is told nothing (the owner lets it lie)."""
    d = hub.s.decision(iid)
    if d:
        if d["state"] != "open":
            return {"kind": "decision", "id": iid, "state": d["state"], "answer": d.get("answer")}
        if answer:
            hub.answer(iid, answer)
            return {"kind": "decision", "id": iid, "state": "answered", "answer": answer}
        hub.s.x("UPDATE decisions SET state='dismissed', answer=?, answered_at=? WHERE id=?",
                (f"{'ignored' if quiet else 'dismissed'} by {by}", now(), iid))
        hub.ack(where="idem=?", args=(f"decision:{iid}",))
        if quiet:
            return {"kind": "decision", "id": iid, "state": "ignored"}
        if d["agent"] and hub.s.agent(d["agent"]):
            hub.s.enqueue(d["agent"], f"Owner closed your question {iid} without answering it (\"{d['question'][:200]}\"): "
                                      "treat it as no longer wanted; ask again only if it still matters.",
                          frm="owner", idem=f"dismiss:{iid}")
        return {"kind": "decision", "id": iid, "state": "dismissed"}
    n = hub.s.one("SELECT * FROM notifications WHERE id=?", (iid,))
    if not n:
        raise LookupError(iid)
    aid = n["agent"]
    if quiet:
        hub.ack(iid)
        return {"kind": "need", "id": iid, "state": "ignored"}
    if answer and n["kind"] == "waiting":
        ch = live_choices(hub, aid)
        if not ch:
            raise ValueError("the terminal no longer shows that choice; open the agent to see where it is")
        k = _pick(answer, ch["options"])
        if k is None:
            raise ValueError("not one of its options: " + " / ".join(ch["options"]))
        if not hub.t.term(aid).choose(k):
            raise ValueError("the terminal's selection would not move; answer it in the terminal")
        hub.ack(iid)
        hub.s.event(aid, "system", f"owner chose {ch['options'][k]!r} in the inbox: {ch['question'][:150]}")
        return {"kind": "need", "id": iid, "state": "answered", "answer": ch["options"][k]}
    hub.ack(iid)
    if n["kind"] == "notify" and aid and hub.s.agent(aid) and not hub.s.agent(aid)["gone"]:
        notice = (n["text"] or "")[:300]
        if answer:
            hub.s.enqueue(aid, f"Owner replied to your notice (\"{notice}\"): {answer}", frm=by, idem=f"reply:{iid}")
            return {"kind": "need", "id": iid, "state": "answered", "answer": answer}
        if hub.t.running(aid) or aid in hub.t.switching:
            hub.s.enqueue(aid, f"Owner acknowledged your notice (\"{notice}\"): seen, no answer given, nothing "
                               "approved by it. Carry on; a decision you still need goes through sb ask.",
                          frm=by, idem=f"ack:{iid}")
    return {"kind": "need", "id": iid, "state": "acked"}
