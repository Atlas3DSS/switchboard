"""Jobs: the one way to launch work (docs/FLEET.md, section 3).

`sb run` records a job owned by an agent and project, the hub places it on a host from the inventory (hosts.py)
after the host's reserve and the reservations of running jobs, and launches it there as a transient user unit
`sb-job-<id>` inside `switchboard-jobs.slice` with CPUWeight, MemoryMax, RuntimeMaxSec, KillMode=control-group
and `ConditionPathExists=!$STATE/HALT`, so a halt raised between placement and start leaves it unstarted. The
unit keeps its result (remain-after-exit) until the hub has read it; then the hub collects it.

States: queued (no host has room, with the reason) -> running -> finished | failed | stopped | lost (unit gone
without a result) | refused (HALT was raised when it tried to start). Adopted jobs are existing units the hub
only watches; they finish when their unit leaves.
"""
import json
import os
import re
import shlex
import time

from . import hosts, placers
from .store import epoch, new_id, now

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:\\-]+\.(service|scope)$")
OPEN = ("queued", "running", "stopping")
FINAL = ("finished", "failed", "stopped", "lost", "refused", "preempted")
LOST_AFTER_S = 3 * hosts.PROBE_S + 10
DEFAULT_HOURS = 12.0
MAX_HOURS = 24 * 30
SLICE_DESCRIPTION = "Switchboard jobs: work launched with sb run (docs/FLEET.md)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, name TEXT, project TEXT, agent TEXT, host TEXT, unit TEXT, cwd TEXT, argv TEXT,
  threads INTEGER, mem_mib INTEGER, gpu_mib INTEGER, gpu_uuid TEXT, hours REAL, state TEXT, reason TEXT,
  adopted INTEGER DEFAULT 0, collect INTEGER DEFAULT 0, pid INTEGER, invocation TEXT, cpu_s REAL, mem_cur_mib INTEGER,
  exit_code INTEGER, created_at TEXT, started_at TEXT, finished_at TEXT, last_seen TEXT, host_pref TEXT, stopped_by TEXT);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, host);
"""


def _row(hub, jid):
    r = hub.s.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not r:
        raise LookupError(f"no job {jid}")
    return dict(r)


def _set(hub, jid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    hub.s.x(f"UPDATE jobs SET {cols} WHERE id=?", (*[json.dumps(v) if isinstance(v, (list, dict)) else v
                                                     for v in kw.values()], jid))


def view(r):
    v = {k: r.get(k) for k in ("id", "name", "project", "agent", "host", "unit", "cwd", "threads", "mem_mib", "gpu_mib",
                               "gpu_uuid", "hours", "state", "reason", "pid", "invocation", "cpu_s", "mem_cur_mib",
                               "exit_code", "created_at", "started_at", "finished_at", "last_seen", "host_pref",
                               "stopped_by")}
    v["adopted"] = bool(r.get("adopted"))
    v["protected"] = bool(r.get("protected"))
    v["board_id"], v["lane"], v["placer"] = r.get("board_id"), r.get("lane"), r.get("placer")
    v["cpus"] = json.loads(r.get("cpus") or "null")
    v["argv"] = json.loads(r.get("argv") or "[]")
    v["open"] = r.get("state") in OPEN
    start, end = epoch(r.get("started_at")), epoch(r.get("finished_at"))
    v["runtime_s"] = int((end or time.time()) - start) if start else None
    return v


# ---------- submit and place ----------
def submit(hub, b):
    """sb run: validate, record, and try to launch right away. Returns the job view (queued if nothing fits)."""
    name = (b.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("--name: letters, digits, . _ - up to 40 characters, starting with a letter or digit")
    argv = b.get("argv") or []
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise ValueError("a command is required after --")
    threads = int(b["threads"]) if b.get("threads") is not None else 1
    mem_mib = int(b["mem_mib"]) if b.get("mem_mib") is not None else 2048
    gpu_mib = int(b["gpu_mib"]) if b.get("gpu_mib") is not None else None
    hours = float(b["hours"]) if b.get("hours") is not None else DEFAULT_HOURS
    if not (1 <= threads <= 4096) or not (64 <= mem_mib <= 16 * 1024 * 1024) or not (0.01 <= hours <= MAX_HOURS):
        raise ValueError("--threads 1..4096, --mem at least 0.0625 GiB, --hours 0.01..720")
    if gpu_mib is not None and not (64 <= gpu_mib <= 4 * 1024 * 1024):
        raise ValueError("--gpu: VRAM in GiB, at least 0.0625")
    cwd = b.get("cwd") or os.path.expanduser("~")
    if not cwd.startswith("/"):
        raise ValueError("--cwd must be absolute")
    lane = b.get("lane") or "batch"
    if lane not in ("batch", "interactive"):
        raise ValueError("--lane is batch or interactive")
    protected = 1 if b.get("protected") else 0
    agent = b.get("agent") or "owner"
    project = b.get("project")
    if not project:
        a = hub.s.agent(agent)
        project = a["project"] if a else None
    cfg = hosts.load_config()
    host_pref = b.get("host") or None
    if host_pref:
        hosts.host(cfg, host_pref)  # raises LookupError
    state = hosts.halt_state(hub)
    if state:
        raise ValueError(f"HALT raised by {state['by']} at {state['at']}: {state.get('reason') or 'no reason'}; "
                         "nothing launches until `sb unhalt`")
    jid = new_id("job")
    hub.s.x("INSERT INTO jobs(id,name,project,agent,host,unit,cwd,argv,threads,mem_mib,gpu_mib,hours,state,reason,"
            "created_at,host_pref,lane,protected) VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,'queued','not placed yet',?,?,?,?)",
            (jid, name, project, agent, f"sb-job-{jid}.service", cwd, json.dumps(argv), threads, mem_mib, gpu_mib,
             hours, now(), host_pref, lane, protected))
    launch_queued(hub, cfg, only=jid)
    return view(_row(hub, jid))


def reserved(hub, host):
    """What running (and launching) jobs on a host already hold."""
    res = {"threads": 0, "mem_mib": 0, "gpu": {}}
    for r in hub.s.q("SELECT threads, mem_mib, gpu_mib, gpu_uuid FROM jobs WHERE host=? AND state IN ('running','stopping')"
                     " AND adopted=0", (host,)):
        res["threads"] += r["threads"] or 0
        res["mem_mib"] += r["mem_mib"] or 0
        if r["gpu_uuid"] and r["gpu_mib"]:
            res["gpu"][r["gpu_uuid"]] = res["gpu"].get(r["gpu_uuid"], 0) + r["gpu_mib"]
    return res


def budget_check(hub, cfg, job):
    """A project's running jobs against its budget in hosts.json ("budgets": {project|default: {...}})."""
    proj = job.get("project") or "-"
    b = cfg.get("budgets", {}).get(proj) or cfg.get("budgets", {}).get("default")
    if not b:
        return None
    use = {"threads": 0, "mem_gib": 0.0, "gpu_gib": 0.0, "jobs": 0}
    for r in hub.s.q("SELECT threads, mem_mib, gpu_mib FROM jobs WHERE project=? AND state IN ('running','stopping') "
                     "AND adopted=0 AND id<>?", (proj, job["id"])):
        use["threads"] += r["threads"] or 0
        use["mem_gib"] += (r["mem_mib"] or 0) / 1024
        use["gpu_gib"] += (r["gpu_mib"] or 0) / 1024
        use["jobs"] += 1
    ask = {"threads": job["threads"], "mem_gib": job["mem_mib"] / 1024, "gpu_gib": (job["gpu_mib"] or 0) / 1024, "jobs": 1}
    for k, cap in b.items():
        if use[k] + ask[k] > cap:
            return f"project {proj} budget: {k} {use[k]:g} used + {ask[k]:g} asked > {cap:g}"
    return None


def place(hub, cfg, job, rows):
    """Choose (host, gpu) for a job from the latest inventory, or (None, None, why)."""
    over = budget_check(hub, cfg, job)
    if over:
        return None, None, over
    candidates = [h for h in cfg["hosts"] if not job["host_pref"] or h["name"] == job["host_pref"]]
    reasons, fallback = [], None
    for h in candidates:
        row = rows.get(h["name"])
        if not row or not row["doc"]:
            reasons.append(f"{h['name']}: not probed yet")
            continue
        if not row["ok"]:
            reasons.append(f"{h['name']}: unreachable ({row['error']})")
            continue
        doc = json.loads(row["doc"])
        cap = hosts.capacity(h, doc)
        held = reserved(hub, h["name"])
        free_threads = cap["free_threads"] - held["threads"]
        free_mem = ((cap["free_ram_gib"] or 0) * 1024) - held["mem_mib"]
        if free_threads < job["threads"]:
            reasons.append(f"{h['name']}: {free_threads} threads free, need {job['threads']}")
            continue
        if free_mem < job["mem_mib"]:
            reasons.append(f"{h['name']}: {int(free_mem)} MiB free, need {job['mem_mib']}")
            continue
        if not job["gpu_mib"]:
            return h, None, None
        cards = [g for g in cap["gpus"] if g["vram_free_mib"] is not None
                 and g["vram_free_mib"] - held["gpu"].get(g["uuid"], 0) >= job["gpu_mib"]]
        if cfg["jobs"]["display_gpu"] == "never":
            cards = [g for g in cards if not g["display"]]
        cards.sort(key=lambda g: -(g["vram_free_mib"] - held["gpu"].get(g["uuid"], 0)))
        plain = [g for g in cards if not g["display"]]
        if plain:
            return h, plain[0], None
        if cards and fallback is None:
            fallback = (h, cards[0])
        reasons.append(f"{h['name']}: no " + ("non-display " if cards else "") + f"GPU with {job['gpu_mib']} MiB free")
    if fallback:
        return fallback[0], fallback[1], None
    return None, None, "; ".join(reasons) or "no host matches"


def _launch_script(cfg, h, doc, job, gpu):
    weight = cfg["jobs"]["slice_cpu_weight"] if h["desktop"] else 100
    total = ((doc.get("memory") or {}).get("total_mib")) or 0
    high = max(1024, int(total - h["reserve"]["ram_gib"] * 1024)) if total else None
    slice_text = f"[Unit]\nDescription={SLICE_DESCRIPTION}\n[Slice]\nCPUWeight={weight}\n" + (
        f"MemoryHigh={high}M\n" if high else "")
    desc = f"sb job {job['name']} ({job['project'] or '-'}/{job['agent']})"
    env = ["-E", f"SB_JOB_ID={job['id']}", "-E", f"SB_AGENT={job['agent']}", "-E", f"SB_PROJECT={job['project'] or ''}",
           "-E", f"SB_HOST={h['name']}"]
    if gpu:
        env += ["-E", f"CUDA_VISIBLE_DEVICES={gpu['uuid']}", "-E", f"SB_GPU={gpu['uuid']}"]
    if job.get("board_id"):
        env += ["-E", f"SB_WORKBOARD_JOB={job['board_id']}"]
    cpus = json.loads(job.get("cpus") or "null")
    if cpus:
        env += ["-p", "AllowedCPUs=" + ",".join(str(c) for c in cpus)]
    run = ["systemd-run", "--user", "--unit", job["unit"].removesuffix(".service"), "--slice", "switchboard-jobs.slice",
           "--remain-after-exit", "--working-directory", job["cwd"], "-p", f"Description={desc}",
           "-p", f"CPUWeight={weight}", "-p", f"MemoryMax={job['mem_mib']}M",
           "-p", f"RuntimeMaxSec={int(job['hours'] * 3600)}", "-p", "KillMode=control-group", "-p", "Nice=10",
           "-p", 'ConditionPathExists=!$S/HALT'] + env + ["--"] + json.loads(job["argv"])
    cmd = " ".join('"ConditionPathExists=!$S/HALT"' if a == 'ConditionPathExists=!$S/HALT' else shlex.quote(a)
                   for a in run)
    return "\n".join([
        "set -u",
        'S="${SB_STATE:-$HOME/.local/state/switchboard}"; D="${SB_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"',
        'if [ -e "$S/HALT" ]; then echo "refused HALT"; exit 0; fi',
        f"SLICE={shlex.quote(slice_text)}",
        'mkdir -p "$D"',
        'if [ ! -e "$D/switchboard-jobs.slice" ] || [ "$(cat "$D/switchboard-jobs.slice")" != "$SLICE" ]; then',
        '  printf %s "$SLICE" > "$D/switchboard-jobs.slice" && systemctl --user daemon-reload && echo "slice written"',
        "fi",
        f"{cmd} 2>&1 && echo launched",
        f"systemctl --user show -p ActiveState,SubState,Result,ConditionResult,MainPID,InvocationID -- {shlex.quote(job['unit'])}",
    ]) + "\n"


def launch_queued(hub, cfg=None, only=None):
    """Place and launch queued jobs, oldest first. Nothing launches while HALT is raised."""
    if hosts.halt_state(hub):
        return []
    cfg = cfg or hosts.load_config()
    rows = {r["name"]: r for r in hub.s.q("SELECT * FROM hosts")}
    q = "SELECT * FROM jobs WHERE state='queued'" + (" AND id=?" if only else "") + " ORDER BY created_at"
    launched = []
    avail = placers.available(cfg)
    hostmap = placers.cpu_run_hostmap(cfg)
    for job in [dict(r) for r in hub.s.q(q, (only,) if only else ())]:
        job["cpus"], job["placer"] = None, None
        if not job["gpu_mib"] and avail["cpu_run"]:
            advice, note = placers.cpu_advise(cfg, job, hostmap)
            if advice:
                pinned = dict(job, host_pref=advice["host"])
                h, gpu, why = place(hub, cfg, pinned, rows)
                if h:
                    job["cpus"], job["placer"] = json.dumps(advice["cpus"]), "cpu-run"
                else:  # cpu-run and the inventory disagree: the inventory wins, without the pin
                    h, gpu, why = place(hub, cfg, job, rows)
            else:
                h, gpu, why = place(hub, cfg, job, rows)
                if h and note:
                    job["placer"] = "hub (" + note + ")"
        else:
            h, gpu, why = place(hub, cfg, job, rows)
        if not h:
            _set(hub, job["id"], reason=why)
            continue
        if gpu and avail["gpu_workboard"] and job.get("adopted", 0) == 0:
            if not job.get("board_id"):
                bid, why = placers.wb_submit(h, cfg, job, gpu["uuid"])
                if not bid and "no gpu-workboard on this host" in (why or ""):
                    job["placer"] = "hub (no gpu-workboard on host)"  # the board is not installed there: hub placement
                elif not bid:
                    _set(hub, job["id"], reason=why)
                    continue
                else:
                    job["board_id"] = bid
                    _set(hub, job["id"], board_id=bid, placer="gpu-workboard")
            if job.get("board_id"):
                ok, assigned, why = placers.wb_acquire(h, cfg, job["board_id"])
                if not ok:
                    _set(hub, job["id"], reason=why)
                    continue
                if assigned and assigned != gpu["uuid"]:
                    cap = hosts.capacity(h, json.loads(rows[h["name"]]["doc"]))
                    gpu = next((g for g in cap["gpus"] if g["uuid"] == assigned), {**gpu, "uuid": assigned})
                job["placer"] = "gpu-workboard"
        doc = json.loads(rows[h["name"]]["doc"])
        _set(hub, job["id"], host=h["name"], gpu_uuid=gpu["uuid"] if gpu else None, state="running",
             started_at=now(), last_seen=now(), reason=None, cpus=job["cpus"],
             placer=job["placer"] or "hub")  # held from now so a parallel launch does not double-book
        rc, out, err = hosts.run_on(h, _launch_script(cfg, h, doc, job, gpu), timeout=60)
        props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l and not l.startswith("Running as unit"))
        if "refused HALT" in out or props.get("ConditionResult") == "no":
            _set(hub, job["id"], state="refused", reason="HALT was raised on the host", finished_at=now(), collect=1)
        elif rc != 0 or "launched" not in out:
            msg = (err.strip() or out.strip()).splitlines()[-1:] or ["launch failed"]
            _set(hub, job["id"], state="failed", reason=f"launch failed: {msg[0][:300]}", finished_at=now(), collect=1)
        else:
            pid = int(props["MainPID"]) if props.get("MainPID", "0").isdigit() and props["MainPID"] != "0" else None
            _set(hub, job["id"], pid=pid, invocation=props.get("InvocationID") or None)
            if job.get("board_id"):
                placers.wb_start(h, cfg, job["board_id"], job["unit"], pid)
            launched.append(job["id"])
        if job.get("board_id") and hub.s.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] in FINAL:
            placers.wb_finish(h, cfg, job["board_id"], "released", "did not start")
    return launched


# ---------- tracking ----------
def _final_from(unit, job):
    if unit.get("condition") == "no":
        return "refused", "HALT was raised on the host"
    if job["state"] == "stopping":
        return "stopped", f"stopped by {job.get('stopped_by') or 'owner'}"
    result = unit.get("result") or ""
    if result == "success":
        return "finished", None
    if result == "timeout":
        return "failed", f"ran past --hours {job['hours']}"
    if result == "oom-kill":
        return "failed", f"out of memory (MemoryMax {job['mem_mib']} MiB)"
    return "failed", f"{result or 'failed'}" + (f", exit {unit['exit']}" if unit.get("exit") is not None else "")


def reconcile(hub):
    """Update every open job from the latest probe document of its host."""
    rows = {r["name"]: r for r in hub.s.q("SELECT * FROM hosts")}
    for job in [dict(r) for r in hub.s.q("SELECT * FROM jobs WHERE state IN ('running','stopping')")]:
        row = rows.get(job["host"])
        if not row or not row["doc"] or not row["ok"]:
            continue  # unreachable host: the job keeps its state; the fleet view shows the host down
        doc = json.loads(row["doc"])
        if epoch(row["probed_at"]) < epoch(job["started_at"] or job["created_at"]):
            continue  # this document predates the launch
        units = {u["unit"]: u for u in (doc.get("units") or []) + (doc.get("jobs") or []) if u.get("unit")}
        u = units.get(job["unit"])
        ts = now()
        if u is None:
            if job.get("board_id") and job["state"] != "stopping":
                b = placers.wb_job_status(hosts.load_config(), job["board_id"])
                if b and b.get("status") == "preempted":
                    _set(hub, job["id"], state="preempted", finished_at=ts,
                         reason="checkpoint preemption by the GPU workboard" + (f": {b.get('reason')}" if b.get("reason") else "")
                                + "; `sb run` it again to resume")
                    continue
            if job["state"] == "stopping":
                _set(hub, job["id"], state="stopped", reason=f"stopped by {job.get('stopped_by') or 'owner'}",
                     finished_at=ts)
            elif job["adopted"]:
                _set(hub, job["id"], state="finished", reason="unit left", finished_at=ts)
            elif time.time() - epoch(job["last_seen"] or job["started_at"] or ts) > LOST_AFTER_S:
                _set(hub, job["id"], state="lost", reason="unit disappeared without a result", finished_at=ts)
            continue
        upd = {"last_seen": ts, "pid": u.get("pid"), "cpu_s": u.get("cpu_s"), "mem_cur_mib": u.get("mem_mib"),
               "invocation": u.get("invocation") or job.get("invocation")}
        if u.get("active") in ("active", "activating", "reloading") and u.get("sub") != "exited":
            if not job["started_at"]:
                upd["started_at"] = ts
            _set(hub, job["id"], **upd)
            continue
        if job["adopted"]:
            _set(hub, job["id"], state="finished", reason=f"unit {u.get('active')}", finished_at=ts, **upd)
            continue
        state, reason = _final_from(u, job)
        if job.get("board_id") and state == "failed" and (u.get("result") or "") in ("signal", "exit-code"):
            b = placers.wb_job_status(hosts.load_config(), job["board_id"])
            if b and b.get("status") == "preempted":
                state, reason = "preempted", ("checkpoint preemption by the GPU workboard; `sb run` it again to resume")
        _set(hub, job["id"], state=state, reason=reason, exit_code=u.get("exit"), finished_at=ts, collect=1, **upd)


def heartbeat(hub, cfg=None):
    """Renew the workboard lease of every running GPU job, with the VRAM the probe saw for its pid."""
    rows = [dict(r) for r in hub.s.q("SELECT id, host, pid, board_id FROM jobs WHERE state='running' AND board_id IS NOT NULL")]
    if not rows:
        return
    cfg = cfg or hosts.load_config()
    docs = {r["name"]: json.loads(r["doc"]) for r in hub.s.q("SELECT name, doc FROM hosts WHERE doc IS NOT NULL")}
    for j in rows:
        try:
            h = hosts.host(cfg, j["host"])
        except LookupError:
            continue
        seen = None
        for g in (docs.get(j["host"]) or {}).get("gpus") or []:
            for p in g.get("procs") or []:
                if j["pid"] and p.get("pid") == j["pid"]:
                    seen = (seen or 0) + (p.get("vram_mib") or 0)
        placers.wb_heartbeat(h, cfg, j["board_id"], j["pid"], seen)


def collect(hub, cfg=None):
    """Remove finished transient units (remain-after-exit kept them for us to read) and close workboard leases."""
    todo = [dict(r) for r in hub.s.q("SELECT id, host, unit, board_id, state, reason FROM jobs WHERE collect=1 AND adopted=0 "
                                     "AND host IS NOT NULL")]
    if not todo:
        return
    cfg = cfg or hosts.load_config()
    by_host = {}
    for j in todo:
        by_host.setdefault(j["host"], []).append(j)
    for name, js in by_host.items():
        try:
            h = hosts.host(cfg, name)
        except LookupError:
            for j in js:
                _set(hub, j["id"], collect=0)
            continue
        # a finished remain-after-exit unit leaves on stop; a failed one leaves on reset-failed
        script = "\n".join((f"systemctl --user reset-failed -- {shlex.quote(j['unit'])} 2>/dev/null; "
                            if j["state"] in ("failed", "lost", "refused", "preempted") else
                            f"systemctl --user stop -- {shlex.quote(j['unit'])} 2>/dev/null; ") + f"echo done {j['id']}"
                           for j in js) + "\n"
        rc, out, err = hosts.run_on(h, script, timeout=60)
        for j in js:
            if f"done {j['id']}" in out:
                _set(hub, j["id"], collect=0)
                if j.get("board_id") and j["state"] != "preempted":
                    outcome = {"finished": "completed", "stopped": "released", "refused": "released"}.get(j["state"], "failed")
                    placers.wb_finish(h, cfg, j["board_id"], outcome, f"{j['state']}: {j.get('reason') or ''}")


def tick(hub):
    reconcile(hub)
    launch_queued(hub)
    heartbeat(hub)
    collect(hub)


def placers_view(hub, cfg):
    """Which backends are present, and the workboard's cards and open leases for the Fleet panel."""
    avail = placers.available(cfg)
    v = {"gpu_workboard": bool(avail["gpu_workboard"]), "cpu_run": bool(avail["cpu_run"])}
    if avail["gpu_workboard"]:
        cache = getattr(hub, "_wb_cache", None)
        if not cache or time.time() - cache[0] > hosts.PROBE_S:
            cache = (time.time(), placers.wb_status(cfg))
            hub._wb_cache = cache
        v["workboard"] = cache[1]
    return v


# ---------- stop, adopt, views ----------
def stop(hub, jid, who, reason=""):
    job = _row(hub, jid)
    if job["state"] in FINAL:
        raise ValueError(f"{jid} is already {job['state']}")
    if job["state"] == "queued":
        _set(hub, jid, state="stopped", reason=f"stopped by {who} before it was placed", finished_at=now(), stopped_by=who)
        if job.get("board_id"):
            try:
                cfg = hosts.load_config()
                placers.wb_cancel(hosts.host(cfg, job["host_pref"]) if job.get("host_pref") else
                                  next(h for h in cfg["hosts"] if h["ssh"] is None), cfg, job["board_id"], f"stopped by {who}")
            except (LookupError, StopIteration):
                pass
        return view(_row(hub, jid))
    cfg = hosts.load_config()
    h = hosts.host(cfg, job["host"])
    _set(hub, jid, state="stopping", stopped_by=who, reason=reason or None)
    rc, out, err = hosts.run_on(h, f"systemctl --user stop -- {shlex.quote(job['unit'])} && echo stopped\n", timeout=90)
    if "stopped" in out:
        _set(hub, jid, state="stopped", reason=f"stopped by {who}" + (f": {reason}" if reason else ""),
             finished_at=now(), collect=0 if job["adopted"] else 1)
    else:
        _set(hub, jid, reason=f"stop requested by {who}; host said: {(err or out).strip()[-200:]}")
    return view(_row(hub, jid))


def adopt(hub, b):
    """Bring an existing unit (or the unit of a pid) under tracking, without relaunching it."""
    name = (b.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("--name: letters, digits, . _ - up to 40 characters")
    cfg = hosts.load_config()
    hname = b.get("host") or next(h["name"] for h in cfg["hosts"] if h["ssh"] is None)
    h = hosts.host(cfg, hname)
    unit, pid = (b.get("unit") or "").strip(), b.get("pid")
    if pid is not None:
        rc, out, err = hosts.run_on(h, f"cat /proc/{int(pid)}/cgroup 2>/dev/null\n", timeout=20)
        hits = re.findall(r"/([^/]+\.(?:service|scope))(?:/|$)", out)
        if not hits:
            raise ValueError(f"pid {pid} on {hname} is not inside a systemd unit; adopt takes a unit name")
        unit = hits[-1]
    if not unit.endswith((".service", ".scope")):
        unit += ".service"
    if not UNIT_RE.match(unit):
        raise ValueError("unit: a systemd unit name")
    rc, out, err = hosts.run_on(h, "systemctl --user show -p ActiveState,SubState,MainPID,InvocationID,ActiveEnterTimestamp "
                                   f"-- {shlex.quote(unit)}\n", timeout=20)
    props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    if props.get("ActiveState") not in ("active", "activating"):
        raise ValueError(f"{unit} on {hname} is {props.get('ActiveState') or 'unknown'}, not running")
    if hub.s.one("SELECT id FROM jobs WHERE host=? AND unit=? AND state IN ('running','stopping')", (hname, unit)):
        raise ValueError(f"{unit} on {hname} is already tracked")
    jid = new_id("job")
    ts = now()
    hub.s.x("INSERT INTO jobs(id,name,project,agent,host,unit,cwd,argv,threads,mem_mib,gpu_mib,hours,state,reason,"
            "adopted,pid,invocation,created_at,started_at,last_seen) VALUES(?,?,?,?,?,?,NULL,'[]',?,?,?,NULL,'running',"
            "'adopted: watched, never halted',1,?,?,?,?,?)",
            (jid, name, b.get("project"), b.get("agent") or "owner", hname, unit, int(b.get("threads") or 0),
             int(b.get("mem_mib") or 0), int(b["gpu_mib"]) if b.get("gpu_mib") else None,
             int(props["MainPID"]) if props.get("MainPID", "0").isdigit() and props["MainPID"] != "0" else None,
             props.get("InvocationID") or None, ts, ts, ts))
    return view(_row(hub, jid))


def list_jobs(hub, include_all=False, project=None, host=None, agent=None, limit=200):
    q, args = "SELECT * FROM jobs", []
    conds = [] if include_all else ["state IN ('queued','running','stopping')"]
    for col, val in (("project", project), ("host", host), ("agent", agent)):
        if val:
            conds.append(f"{col}=?")
            args.append(val)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY (state IN ('queued','running','stopping')) DESC, created_at DESC LIMIT ?"
    args.append(limit)
    return {"jobs": [view(dict(r)) for r in hub.s.q(q, tuple(args))]}


def show(hub, jid, log_lines=40):
    job = _row(hub, jid)
    v = view(job)
    if job["host"] and job["unit"] and log_lines:
        try:
            h = hosts.host(hosts.load_config(), job["host"])
            rc, out, err = hosts.run_on(h, f"journalctl --user -u {shlex.quote(job['unit'])} -n {int(log_lines)} "
                                           "--no-pager -o short-iso 2>/dev/null || true\n", timeout=30)
            v["log"] = out[-20000:]
        except LookupError:
            v["log"] = f"(host {job['host']} is no longer in the hosts file)"
    return v


def for_host(hub, name):
    return [view(dict(r)) for r in hub.s.q("SELECT * FROM jobs WHERE host=? AND state IN ('queued','running','stopping')"
                                            " ORDER BY created_at", (name,))]


def terminal_pids(hub):
    """pid -> agent id for every agent terminal the hub runs or observes on this machine."""
    out = {}
    try:
        for aid, t in list(getattr(hub.t, "terms", {}).items()):
            for key in ("pid", "holder_pid"):
                p = (getattr(t, "state", None) or {}).get(key)
                if p:
                    out[int(p)] = aid
    except Exception:  # noqa: BLE001 - attribution is best effort
        pass
    try:
        from . import discovery
        panes = {p["target"]: p for p in discovery.tmux_panes()}
        for a in hub.s.agents():
            p = panes.get(a.get("pane") or "")
            if p:
                for pid in p["pids"]:
                    out.setdefault(int(pid), a["id"])
    except Exception:  # noqa: BLE001
        pass
    return out
