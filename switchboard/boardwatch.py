"""Watch the GPU workboard's sight of each host .

The board schedules a card only while its telemetry is fresh (telemetry_stale_seconds, 120). A host whose telemetry
timer is off looks exactly like a full host: 0 GiB schedulable, nothing admitted, and so no job heartbeat ever brings a
snapshot back. A host can sit dark like that for hours with idle GPUs and nobody is told.

Every fleet tick: a host the hub can reach whose cards the board calls stale gets its telemetry timer enabled and one
publish, at once. If the cards are still stale PAGE_S after they were first seen so, the owner is paged; they are told
again when the board sees the host once more.
"""
import time

from . import hosts

PAGE_S = 240  # stale this long despite the repair: page the owner
RETRY_S = 120  # between repair attempts while it stays stale
UNIT = "gpu-workboard-telemetry"
REPAIR = (f"systemctl --user enable --now {UNIT}.timer >/dev/null 2>&1\n"
          f"systemctl --user start {UNIT}.service >/dev/null 2>&1\n"
          f"echo timer=$(systemctl --user is-enabled {UNIT}.timer 2>&1 | head -1)/"
          f"$(systemctl --user is-active {UNIT}.timer 2>&1 | head -1) "
          f"publish=$(systemctl --user show -p Result --value {UNIT}.service 2>&1 | head -1)\n")


def stale_hosts(wb, rows):
    """{hub host name: [stale card labels]} for hosts the hub reached on its last probe."""
    stale = {c.get("uuid"): c.get("label") or c.get("uuid") for c in (wb or {}).get("cards") or []
             if c.get("uuid") and c.get("fresh") is False}
    out = {}
    for name, row in rows.items():
        if not row.get("ok"):
            continue  # unreachable is the Fleet panel's own alarm, and nothing can be repaired there
        mine = [stale[g["uuid"]] for g in (row.get("doc") or {}).get("gpus") or [] if g.get("uuid") in stale]
        if mine:
            out[name] = mine
    return out


def check(hub, cfg, wb, rows, now=None, run=hosts.run_on):
    """One pass. rows: {host name: {"ok": bool, "doc": probe doc}}. Returns what it did, for the log and the tests."""
    now = time.time() if now is None else now
    state = hub.__dict__.setdefault("_wb_stale", {})
    did = []
    if not wb or wb.get("error"):
        return did  # the board itself did not answer: no verdict on any host
    bad = stale_hosts(wb, rows)
    for name in [n for n in state if n not in bad]:
        was = state.pop(name)
        if was.get("paged"):
            hub.notify(None, f"GPU scheduler sees {name} again after {int((now - was['since']) / 60)} min.",
                       level="info", project="switchboard")
        did.append(("recovered", name))
    byname = {h["name"]: h for h in cfg["hosts"]}
    for name, cards in bad.items():
        st = state.setdefault(name, {"since": now, "tried": 0, "note": ""})
        if now - st["tried"] >= RETRY_S and name in byname:
            st["tried"] = now
            rc, out, err = run(byname[name], REPAIR, timeout=40)
            st["note"] = (out.strip() or err.strip() or f"exit {rc}")[-160:]
            print(f"boardwatch: {name}: {len(cards)} stale card(s); repair: {st['note']}", flush=True)
            did.append(("repair", name))
        if not st.get("paged") and now - st["since"] >= PAGE_S:
            st["paged"] = True
            hub.notify(None, f"GPU scheduler cannot see {name}: {len(cards)} card(s) stale for "
                             f"{int((now - st['since']) / 60)} min, so nothing can start there. Enabling the telemetry "
                             f"timer did not fix it ({st['note'] or 'no answer'}).",
                       project="switchboard", idem=f"wb-stale:{name}:{int(st['since'])}")
            did.append(("paged", name))
    return did


def tick(hub, cfg=None):
    import json

    from . import jobs
    cfg = cfg or hosts.load_config()
    wb = jobs.placers_view(hub, cfg).get("workboard")
    if not wb:
        return []
    rows = {r["name"]: {"ok": r["ok"], "doc": json.loads(r["doc"]) if r["doc"] else {}}
            for r in hub.s.q("SELECT name, doc, ok FROM hosts")}
    return check(hub, cfg, wb, rows)
