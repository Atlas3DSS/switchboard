"""An idle GPU lease loses its standing (so GPUs do not sit idle behind leases nobody is using).

Every open workboard lease has a standing that starts at 1 and halves for every HALF_LIFE of GPU idleness on its
card (utilisation under UTIL_MIN at every probe, and no work reported on the lease's heartbeat within a
half-life); any use restores it in full. Nothing exempts a lease: not its
lane, not a priority project, not the owner's own request. At each halving the lease's agent is asked whether it
still needs the card and told to give it back if not. After RELEASE_HALVINGS halvings the lease has no standing
left and the hub gives the card back itself: a hub job is stopped (which closes its lease), a lease taken on the
board directly is finished as released. Idle time only accrues on a fresh probe that shows the card idle, so an
unreachable host or a hub restart never counts against a lease.

hosts.json "jobs": "lease_half_life_min" (default 15); "lease_release": false keeps the asking but never takes a
card back; "lease_decay": false turns it all off.
"""
import json
import time

from . import hosts, jobs, placers
from .store import epoch

EVERY_S = 60
UTIL_MIN = 5            # percent; under this at a probe, the card is idle
RELEASE_HALVINGS = 3    # standing 1/8: the card is taken back
SETTING = "lease_decay"

ASK = ("Switchboard lease check: your GPU lease {label} ({bid}) on {card} has sat idle for {idle} min (GPU use "
       "under {util}%). Its standing is down to {pct}% and halves every {half} min of idleness; at {gone} min idle "
       "Switchboard gives the card back for you. Be conscientious: other work is waiting on these cards. Does this "
       "work still need the card right now? If not, give it back now ({how}). If it does, put the card to work; "
       "only real use restores the standing. No reply needed.")
GONE = ("Switchboard lease check: your GPU lease {label} ({bid}) on {card} sat idle for {idle} min and has lost "
        "its standing, so the card was given back ({what}). Ask for it again with `sb run` when the work is ready "
        "to use it.")


def standing(idle_s, half_s):
    return 0.5 ** (max(0.0, idle_s) / half_s)


def card_util(hub, now):
    """uuid -> (utilisation %, label) from every host probed recently enough to trust."""
    out = {}
    for r in hub.s.q("SELECT name, doc, probed_at, ok FROM hosts WHERE doc IS NOT NULL"):
        if not r["ok"] or now - epoch(r["probed_at"]) > 3 * hosts.PROBE_S + 10:
            continue
        for g in (json.loads(r["doc"]) or {}).get("gpus") or []:
            if g.get("uuid") and g.get("util") is not None:
                out[g["uuid"]] = (g["util"], f"{r['name']} GPU {g.get('index')}")
    return out


def _agent_for(hub, job, lease):
    """Who answers for a lease: the hub job's agent, else whoever answers for the lease's project."""
    aid = (job or {}).get("agent")
    a = hub.s.agent(aid) if aid and aid != "owner" else None
    if a and not a.get("gone") and a["provider"] != "shell":
        return aid
    pid = (job or {}).get("project") or lease.get("project")
    return hub.lead_of(pid) if pid and hub.s.project(pid) else None


def _tell(hub, aid, text, idem, lease):
    if aid:
        hub.s.enqueue(aid, text, frm="owner", idem=idem)
    else:  # nobody answers for it: the owner hears instead
        hub.notify(None, text.split(". Be conscientious")[0][:300], kind="lease", level="info", idem=idem,
                   project=lease.get("project"))


def tick(hub, now=None, status=None, cfg=None):
    """One pass. Returns [(board_id, 'asked'|'released')] for what it did."""
    cfg = cfg or hosts.load_config()
    jcfg = cfg.get("jobs") or {}
    if jcfg.get("lease_decay") is False:
        return []
    st = status if status is not None else jobs.placers_view(hub, cfg).get("workboard")
    if not st or st.get("error"):
        return []
    now = now or time.time()
    half = float(jcfg.get("lease_half_life_min") or 15) * 60
    util = card_util(hub, now)
    state, keep, did = hub.s.setting(SETTING, {}) or {}, {}, []
    for lease in st.get("jobs") or []:
        bid, uuid = lease.get("id"), lease.get("uuid")
        if lease.get("status") != "running" or not bid or not uuid:
            continue
        e = state.get(bid) or {"idle_s": 0.0, "at": now, "asked": 0}
        dt, e["at"] = min(max(0.0, now - e["at"]), 2 * EVERY_S), now
        keep[bid] = e
        if uuid not in util:
            continue  # no fresh look at the card: the clock stands still
        # a spot probe once a minute misses work done in sub-second bursts; the holder's own heartbeat counts
        # requests, and the board moves work_changed when they rise, so reported work within a half-life is use
        worked = (lease.get("work_changed") or 0) >= now - half
        if worked or util[uuid][0] >= UTIL_MIN:
            e["idle_s"], e["asked"] = 0.0, 0
            continue
        e["idle_s"] += dt
        halvings = int(e["idle_s"] // half)
        if halvings <= e["asked"]:
            continue
        row = hub.s.one("SELECT id, agent, project, name, state FROM jobs WHERE board_id=? ORDER BY created_at DESC LIMIT 1",
                        (bid,))
        job = dict(row) if row else None
        aid = _agent_for(hub, job, lease)
        live = job and job["state"] == "running"
        fmt = {"label": lease.get("label") or (job or {}).get("name") or "-", "bid": bid, "card": util[uuid][1],
               "idle": int(e["idle_s"] // 60), "util": UTIL_MIN, "half": int(half // 60),
               "gone": int(RELEASE_HALVINGS * half // 60), "pct": round(100 * standing(e["idle_s"], half), 1)}
        if halvings >= RELEASE_HALVINGS and jcfg.get("lease_release") is not False:
            what = _release(hub, cfg, job if live else None, bid, fmt["idle"])
            _tell(hub, aid, GONE.format(what=what, **fmt), f"lease-gone:{bid}", lease)
            keep.pop(bid, None)
            did.append((bid, "released"))
            continue
        how = f"`sb stop {job['id']}`" if live else f"stop its process, then `gpu-workboard finish {bid} --outcome released`"
        _tell(hub, aid, ASK.format(how=how, **fmt), f"lease-idle:{bid}:{halvings}", lease)
        e["asked"] = halvings
        did.append((bid, "asked"))
    hub.s.set_setting(SETTING, keep)
    return did


def _release(hub, cfg, job, bid, idle_min):
    why = f"GPU lease idle {idle_min} min: no standing left"
    if job:
        try:
            jobs.stop(hub, job["id"], "switchboard", why)
            return f"job {job['id']} stopped"
        except (LookupError, ValueError) as e:
            return f"stopping job {job['id']} failed: {e}"
    local = next((h for h in cfg["hosts"] if h["ssh"] is None), None)
    ok, text = placers.wb_finish(local, cfg, bid, "released", why) if local else (False, "no local host")
    return "lease released on the board; its process, if any, is yours to stop" if ok else \
        f"the board refused the release: {(text or '')[:160]}"


def view(hub, cfg=None):
    """board_id -> {idle_min, standing} for leases that are idle right now (Fleet panel, sb fleet)."""
    cfg = cfg or hosts.load_config()
    half = float((cfg.get("jobs") or {}).get("lease_half_life_min") or 15) * 60
    return {bid: {"idle_min": int(e["idle_s"] // 60), "standing": round(standing(e["idle_s"], half), 3)}
            for bid, e in (hub.s.setting(SETTING, {}) or {}).items() if e.get("idle_s", 0) >= 60}
