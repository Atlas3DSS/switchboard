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
import math
import os
import re
import shlex
import threading
import time

from . import hosts, placers
from .store import epoch, new_id, now

_launching, _launching_lock = set(), threading.Lock()  # jobs a launch_queued call is working on right now

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:\\-]+\.(service|scope)$")
OPEN = ("queued", "running", "stopping")
FINAL = ("finished", "failed", "stopped", "lost", "refused", "preempted")
LOST_AFTER_S = 3 * hosts.PROBE_S + 10
DEFAULT_HOURS = 12.0
MAX_HOURS = 24 * 30
# sb itself, for jobs that call `sb send` / `sb notify`: a unit gets the user manager's PATH, which lacks this
# directory, so without it `sb` is "No such file or directory" inside every job.
SB_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
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
    v["gpu_pin"] = r.get("gpu_pin")
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
    lane = b.get("lane")
    if lane not in (None, "batch", "interactive"):
        raise ValueError("--lane is batch or interactive")
    protected = 1 if b.get("protected") else 0
    gpu_pin = (b.get("gpu_uuid") or "").strip() or None
    if gpu_pin and not gpu_mib:
        raise ValueError("--gpu-uuid needs --gpu (the VRAM the job holds on that card)")
    if gpu_pin and not re.fullmatch(r"GPU-[\w-]+", gpu_pin):
        raise ValueError("--gpu-uuid: a card UUID as nvidia-smi prints it (GPU-...)")
    agent = b.get("agent") or "owner"
    project = b.get("project")
    if not project:
        a = hub.s.agent(agent)
        project = a["project"] if a else None
    cfg = hosts.load_config()
    if lane is None:  # a priority project's GPU job takes the board's interactive lane, which can checkpoint batch work
        lane = "interactive" if project in (cfg["jobs"].get("priority_projects") or []) else "batch"
    host_pref = b.get("host") or None
    if host_pref:
        hosts.host(cfg, host_pref)  # raises LookupError
    state, halted = hosts.halt_state(hub), hosts.halted_hosts(hub, cfg)
    if state and (host_pref in halted if host_pref else not [h for h in cfg["hosts"] if h["name"] not in halted]):
        where = f"on {host_pref}" if host_pref else "on every host"
        raise ValueError(f"HALT raised by {state['by']} at {state['at']} {where}: {state.get('reason') or 'no reason'}; "
                         "nothing launches there until `sb unhalt`")
    jid = new_id("job")
    hub.s.x("INSERT INTO jobs(id,name,project,agent,host,unit,cwd,argv,threads,mem_mib,gpu_mib,hours,state,reason,"
            "created_at,host_pref,lane,protected,gpu_pin) VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,'queued','not placed yet',"
            "?,?,?,?,?)",
            (jid, name, project, agent, f"sb-job-{jid}.service", cwd, json.dumps(argv), threads, mem_mib, gpu_mib,
             hours, now(), host_pref, lane, protected, gpu_pin))
    launch_queued(hub, cfg, only=jid)
    return view(_row(hub, jid))


MEM_SETTLE_S = 20 * 60          # a job holds its full declared memory for its first 20 minutes
MEM_HEADROOM, MEM_HEADROOM_MIB = 1.25, 2048


def mem_hold_mib(declared, peak, started_at, now=None):
    """RAM the hub books for a running job. Until it has run MEM_SETTLE_S: its declaration. After that: its observed
    peak (the unit's MemoryPeak) plus headroom (x1.25, at least 2 GiB), never above the declaration. The unit keeps
    MemoryMax = its declaration, so the job can still grow to it; MemAvailable is still checked live in place().
    Why: a job that declares 96 GiB and peaks at 7.5 GiB would otherwise make a host with ~180 GiB available look
    full, and its GPUs sit idle while other jobs wait."""
    if peak is None or not started_at:
        return declared
    if (time.time() if now is None else now) - epoch(started_at) < MEM_SETTLE_S:
        return declared
    return min(declared, max(int(peak * MEM_HEADROOM), peak + MEM_HEADROOM_MIB))


CPU_SETTLE_S = 20 * 60          # a job holds its declared threads for its first 20 minutes
CPU_HEADROOM = 1.25
GPU_SETTLE_S = 20 * 60
GPU_HEADROOM, GPU_HEADROOM_MIB = 1.25, 2048


def thread_hold(declared, cpu_s, started_at, now=None):
    """Threads the hub books for a running job so an over-estimate is clawed back without
    touching the process. Until CPU_SETTLE_S: its
    declaration. After that: its average use since start (the unit's CPUUsageNSec / run time) x1.25, rounded up, at
    least 1, never above the declaration. Nothing is changed on the unit: its AllowedCPUs/limits stay, so it can still
    use what it declared; live load is still counted in place(), and fleet-priority freezes lower tiers on contention."""
    if cpu_s is None or not started_at or not declared:
        return declared
    ran = (time.time() if now is None else now) - epoch(started_at)
    if ran < CPU_SETTLE_S:
        return declared
    return min(declared, max(1, math.ceil(cpu_s / ran * CPU_HEADROOM)))


def gpu_hold_mib(held, realized, settled):
    """VRAM booked on a card once every job holding it has run GPU_SETTLE_S: what is really there x1.25 (at least
    +2 GiB), never above the declarations. Before that, or with no reading, the declarations."""
    if not settled or realized is None:
        return held
    return min(held, max(int(realized * GPU_HEADROOM), realized + GPU_HEADROOM_MIB))


def reserved(hub, host, doc=None):
    """What running (and launching) jobs on a host hold: declared totals, and the part not yet visible in the
    probe (declared minus the unit's measured memory, declared minus the card's measured VRAM).

    VRAM is realized per card, not per pid: a job's VRAM usually sits in a child of the unit's main pid, and
    nvidia-smi on Windows/WSL attributes nothing to any pid, so matching the job's pid found 0 MiB realized,
    the whole lease stayed 'unrealized' on top of the real usage, and every request on that card was refused
    with VRAM physically free. Realized on a card = the VRAM of procs
    that match a job pid, or, when none match, the card's measured use, capped at what the jobs hold."""
    res = {"threads": 0, "mem_mib": 0, "mem_unrealized_mib": 0, "gpu": {}, "gpu_unrealized": {}}
    vram_by_pid, used_by_card = {}, {}
    for g in (doc or {}).get("gpus") or []:
        used_by_card[g["uuid"]] = g.get("vram_used_mib") or 0
        for p in g.get("procs") or []:
            if p.get("pid"):
                vram_by_pid[(g["uuid"], p["pid"])] = vram_by_pid.get((g["uuid"], p["pid"]), 0) + (p.get("vram_mib") or 0)
    attributed = {}
    peaks = {u["unit"]: u.get("mem_peak_mib") for u in ((doc or {}).get("units") or []) + ((doc or {}).get("jobs") or [])
             if u.get("unit")}
    cpu_s = {u["unit"]: u.get("cpu_s") for u in ((doc or {}).get("units") or []) + ((doc or {}).get("jobs") or [])
             if u.get("unit")}
    unsettled = set()
    for r in hub.s.q("SELECT threads, mem_mib, mem_cur_mib, gpu_mib, gpu_uuid, pid, unit, started_at FROM jobs WHERE host=? "
                     "AND state IN ('running','stopping') AND adopted=0", (host,)):
        res["threads"] += thread_hold(r["threads"] or 0, cpu_s.get(r["unit"]), r["started_at"])
        res["mem_mib"] += r["mem_mib"] or 0
        res["mem_unrealized_mib"] += max(0, mem_hold_mib(r["mem_mib"] or 0, peaks.get(r["unit"]), r["started_at"])
                                         - (r["mem_cur_mib"] or 0))
        if r["gpu_uuid"] and r["gpu_mib"]:
            res["gpu"][r["gpu_uuid"]] = res["gpu"].get(r["gpu_uuid"], 0) + r["gpu_mib"]
            if not r["started_at"] or time.time() - epoch(r["started_at"]) < GPU_SETTLE_S:
                unsettled.add(r["gpu_uuid"])
            seen = vram_by_pid.get((r["gpu_uuid"], r["pid"]), 0) if r["pid"] else 0
            attributed[r["gpu_uuid"]] = attributed.get(r["gpu_uuid"], 0) + seen
    for uuid, held in res["gpu"].items():
        realized = attributed.get(uuid) or min(held, used_by_card.get(uuid, 0))
        res["gpu_unrealized"][uuid] = max(0, gpu_hold_mib(held, realized, uuid not in unsettled) - realized)
    return res


def pinned_cpus(doc, but=None):
    """The CPUs something is really running on, from the host's last probe (probe.pins): every sb job's processes,
    and anything else pinned that has used a twentieth of a CPU or more since it started. `but`: a unit to leave
    out (the job asking)."""
    taken = set()
    for p in (doc or {}).get("pins") or []:
        unit = p.get("unit") or ""
        if unit == but or not (unit.startswith("sb-job-") or (p.get("cpu") or 0) >= 5):
            continue
        taken.update(p.get("cpus") or [])
    for group in ((doc or {}).get("cpu") or {}).get("siblings") or []:  # the other thread of a core in use is not free
        if taken.intersection(group):
            taken.update(group)
    return taken


def disjoint_cpus(hub, host, nthreads, advised, want, doc=None):
    """The CPU set a job is pinned to: cpu-run's advice when none of it is held by another running sb job on the
    host, else the lowest free CPUs. cpu-run's dry run is an advisory snapshot that sees live load, not the hub's
    pins, so every 2-thread job it advised landed on CPUs 0,1 (the shared-affinity trap). When the
    host has too few free CPUs the advice stands: the inventory already found the threads."""
    taken = set()
    seen = {p.get("unit") for p in (doc or {}).get("pins") or []}
    for r in hub.s.q("SELECT cpus, unit FROM jobs WHERE host=? AND state IN ('running','stopping') AND cpus IS NOT NULL",
                     (host,)):
        if r["unit"] not in seen:  # a job the probe has seen is counted where it really is, not where it was told
            taken.update(json.loads(r["cpus"]) or [])
    # ... and whatever is really on a CPU, tracked or not: a GPU job records no CPUs and a command may pin itself
    # somewhere other than its label
    taken |= pinned_cpus(doc)
    if advised and not taken.intersection(advised):
        return advised
    free = [c for c in range(nthreads or 0) if c not in taken]
    return free[:want] if len(free) >= want else advised


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


TINY_HOURS = 1.0  # a 1-thread CPU job this short never waits on load (jobs.cuts_in)


OTHER_TIER = 99


def tier(cfg, project):
    """A project's rank: its place in hosts.json jobs.priority_projects; 0 is first. Everything else ranks after the list."""
    order = cfg.get("jobs", {}).get("priority_projects") or []
    return order.index(project) if project in order else OTHER_TIER


def cuts_in(cfg, job):
    """Why a job goes ahead of the queue and past the thread gate, or None. Two kinds:
    a priority project's job (hosts.json jobs.priority_projects; none by default), and a tiny job from any
    project (1 thread, no GPU, at most TINY_HOURS). The thread gate counts live load, so a box oversubscribed by
    long runs would hold an hourly 1-CPU-second job for minutes each hour. Cutting in is graceful: nothing
    running is stopped, the kernel shares the CPUs, and memory, VRAM, HALT and budgets still apply."""
    if job.get("project") in (cfg.get("jobs", {}).get("priority_projects") or []):
        return "priority project"
    if (job.get("threads") or 0) <= 1 and not job.get("gpu_mib") and job.get("hours") is not None \
            and job["hours"] <= TINY_HOURS:
        return "tiny job"
    return None


def place(hub, cfg, job, rows, board=False):
    """Choose (host, gpu) for a job from the latest inventory, or (None, None, why). A job that cuts in (cuts_in)
    still goes where there is room when some host has it, and only when none has does it take a full one."""
    h, gpu, why = _place(hub, cfg, job, rows, board, cut=False)
    if h or not cuts_in(cfg, job):
        return h, gpu, why
    h2, gpu2, why2 = _place(hub, cfg, job, rows, board, cut=True)
    return (h2, gpu2, None) if h2 else (None, None, why2)


def _place(hub, cfg, job, rows, board, cut):
    """One pass of place(); cut: skip the thread gate (live load and held threads).

    board: the gpu-workboard is in use. An interactive-lane job then reaches the board even when the inventory
    says every card is full, because the board's interactive lane can checkpoint a preemptible batch job to make
    room; the inventory alone would keep such a job queued forever behind the batch job. The
    pick is provisional (job["board_only"] says why) and the board's lease decides the card."""
    over = budget_check(hub, cfg, job)
    if over:
        return None, None, over
    candidates = [h for h in cfg["hosts"] if not job["host_pref"] or h["name"] == job["host_pref"]]
    reasons, fallback = [], None
    for h in candidates:
        if h["name"] in cfg.get("halted", ()):
            reasons.append(f"{h['name']}: HALT raised")
            continue
        row = rows.get(h["name"])
        if not row or not row["doc"]:
            reasons.append(f"{h['name']}: not probed yet")
            continue
        if not row["ok"]:
            reasons.append(f"{h['name']}: unreachable ({row['error']})")
            continue
        doc = json.loads(row["doc"])
        cap = hosts.capacity(h, doc)
        held = reserved(hub, h["name"], doc)
        # Live load and MemAvailable already include what running jobs use, so a job's declared hold counts only as
        # far as it is not yet realized: threads = max(load, declared), memory and VRAM = declared minus measured.
        per_core = (cap["threads"] // cap["cores"]) if cap["cores"] else 1
        busy = max(int(round((cap["load"] or [0])[0] or 0)), held["threads"])
        free_threads = max(0, cap["threads"] - h["reserve"]["cores"] * per_core - busy)
        free_mem = max(0, ((cap["ram_available_gib"] or 0) * 1024) - h["reserve"]["ram_gib"] * 1024 - held["mem_unrealized_mib"])
        if free_threads < job["threads"] and not cut:
            reasons.append(f"{h['name']}: {free_threads} threads free, need {job['threads']}")
            continue
        if free_mem < job["mem_mib"]:
            short = f"{h['name']}: RAM {int(free_mem)} MiB free, need {job['mem_mib']}"
            # the first-tier project is never blocked by a lack of RAM: it
            # places anyway; its launch asks fleet-priority on that host to stop lower tiers until the RAM is there
            if not job["gpu_mib"] and tier(cfg, job.get("project")) == 0 and \
                    (cap["ram_available_gib"] or 0) * 1024 >= job["mem_mib"] * 0.25:
                job["ram_warn"] = "warning (fleet-priority frees RAM): " + short
                return h, None, None
            if not job["gpu_mib"]:
                reasons.append(short)
                continue
            # for a GPU job RAM is a warning, never a blocker; a free card is not held for RAM
            job["ram_warn"] = "warning: " + short
        if not job["gpu_mib"]:
            return h, None, None
        pin = job.get("gpu_pin")
        if pin and pin not in [g["uuid"] for g in cap["gpus"]]:
            reasons.append(f"{h['name']}: no card {pin}")
            continue
        cards = [g for g in cap["gpus"] if g["vram_free_mib"] is not None and (not pin or g["uuid"] == pin)
                 and g["vram_free_mib"] - held["gpu_unrealized"].get(g["uuid"], 0) >= job["gpu_mib"]]
        if cfg["jobs"]["display_gpu"] == "never":
            cards = [g for g in cards if not g["display"]]
        cards.sort(key=lambda g: -(g["vram_free_mib"] - held["gpu_unrealized"].get(g["uuid"], 0)))
        plain = [g for g in cards if not g["display"]]
        if plain:
            return h, plain[0], None
        if cards and fallback is None:
            fallback = (h, cards[0])
        why = f"{h['name']}: " + (f"card {pin} lacks" if pin else "no " + ("non-display " if cards else "") + "GPU with") \
            + f" {job['gpu_mib']} MiB free"
        reasons.append(why)
        if board and (job.get("lane") == "interactive" or cuts_in(cfg, job) == "priority project") and fallback is None:
            pool = [g for g in cap["gpus"] if g["vram_free_mib"] is not None and (not pin or g["uuid"] == pin)
                    and (cfg["jobs"]["display_gpu"] != "never" or not g["display"])]
            if pool:
                pool.sort(key=lambda g: (g["display"], -(g["vram_free_mib"] - held["gpu_unrealized"].get(g["uuid"], 0))))
                job["board_only"] = why
                fallback = (h, pool[0])
    if fallback:
        return fallback[0], fallback[1], None
    return None, None, "; ".join(reasons) or "no host matches"


def _launch_script(cfg, h, doc, job, gpu):
    weight = cfg["jobs"]["slice_cpu_weight"] if h["desktop"] else 100
    rank = tier(cfg, job.get("project"))
    # priority work must not run below every desktop service; per-tier niceness and unit weight
    nice, unit_weight = {0: (0, 1000), 1: (4, 400), 2: (7, 200)}.get(rank, (10, weight))
    total = ((doc.get("memory") or {}).get("total_mib")) or 0
    high = max(1024, int(total - h["reserve"]["ram_gib"] * 1024)) if total else None
    slice_text = f"[Unit]\nDescription={SLICE_DESCRIPTION}\n[Slice]\nCPUWeight={weight}\n" + (
        f"MemoryHigh={high}M\n" if high else "")
    slice_name = "switchboard-jobs.slice"
    if rank == 0:  # the first-tier project's own slice: it competes with app.slice as an equal, not at the jobs weight
        slice_name = "switchboard-priority.slice"
        slice_text = f"[Unit]\nDescription={SLICE_DESCRIPTION} (first-tier project)\n[Slice]\nCPUWeight=1000\n"
    desc = f"sb job {job['name']} ({job['project'] or '-'}/{job['agent']})"
    env = ["-E", f"SB_JOB_ID={job['id']}", "-E", f"SB_AGENT={job['agent']}", "-E", f"SB_PROJECT={job['project'] or ''}",
           "-E", f"SB_HOST={h['name']}", "-E", "SB=" + os.path.join(SB_BIN, "sb"), "-E", "__SB_PATH__"]
    if gpu:
        env += ["-E", f"CUDA_VISIBLE_DEVICES={gpu['uuid']}", "-E", f"SB_GPU={gpu['uuid']}"]
    if job.get("board_id"):
        env += ["-E", f"SB_WORKBOARD_JOB={job['board_id']}"]
    cpus = json.loads(job.get("cpus") or "null")
    argv = json.loads(job["argv"])
    if cpus:
        env += ["-p", "AllowedCPUs=" + ",".join(str(c) for c in cpus)]
        if not doc.get("cpuset"):  # this host does not enforce AllowedCPUs for user units: pin the command itself
            argv = ["taskset", "-c", ",".join(str(c) for c in cpus)] + argv
    run = ["systemd-run", "--user", "--unit", job["unit"].removesuffix(".service"), "--slice", slice_name,
           "--remain-after-exit", "--working-directory", job["cwd"], "-p", f"Description={desc}",
           "-p", f"CPUWeight={unit_weight}", "-p", f"MemoryMax={job['mem_mib']}M",
           "-p", f"RuntimeMaxSec={int(job['hours'] * 3600)}", "-p", "KillMode=control-group", "-p", f"Nice={nice}",
           "-p", 'ConditionPathExists=!$S/HALT'] + env + ["--"] + argv
    live = {'ConditionPathExists=!$S/HALT': '"ConditionPathExists=!$S/HALT"', "__SB_PATH__": '"PATH=$P"'}
    cmd = " ".join(live.get(a) or shlex.quote(a) for a in run)
    return "\n".join([
        "set -u",
        'S="${SB_STATE:-$HOME/.local/state/switchboard}"; D="${SB_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"',
        'if [ -e "$S/HALT" ]; then echo "refused HALT"; exit 0; fi',
        # the unit's PATH: the user manager's (what a job got before), with sb's directory in front
        "P=" + shlex.quote(SB_BIN) + ':"$(systemctl --user show-environment 2>/dev/null | sed -n "s/^PATH=//p")"',
        '[ "$P" = ' + shlex.quote(SB_BIN + ":") + ' ] && P="$P/usr/local/bin:/usr/bin:/bin"',
        f"SLICE={shlex.quote(slice_text)}",
        'mkdir -p "$D"',
        f"SN={shlex.quote(slice_name)}",
        'if [ ! -e "$D/$SN" ] || [ "$(cat "$D/$SN")" != "$SLICE" ]; then',
        '  printf %s "$SLICE" > "$D/$SN" && systemctl --user daemon-reload && echo "slice written"',
        "fi",
    ] + ([
        # ask fleet-priority (an optional per-host daemon that reads this folder) on this host to free the job's RAM from lower tiers for its first
        # 20 minutes (after that the job holds what it took, and MemAvailable no longer shows the need)
        'F="$HOME/.local/state/fleet-priority/demand"; mkdir -p "$F" && printf %s ' + shlex.quote(json.dumps({
            "tier": "first", "ram_gib": round(job["mem_mib"] / 1024, 1), "why": f"sb job {job['name']}"}))
        + f' | python3 -c "import json,sys,time; d=json.load(sys.stdin); d[\'until\']=time.time()+{min(int(job["hours"] * 3600), CPU_SETTLE_S)};'
        + f' print(json.dumps(d))" > "$F/sb-{job["id"]}.json" || true',
    ] if rank == 0 else []) + [
        # systemd-run resolves a relative command (./script.sh) against its own cwd, not --working-directory
        f"cd {shlex.quote(job['cwd'])} || {{ echo launch failed: no directory {shlex.quote(job['cwd'])}; exit 1; }}",
        f"{cmd} 2>&1 && echo launched",
        f"systemctl --user show -p ActiveState,SubState,Result,ConditionResult,MainPID,InvocationID -- {shlex.quote(job['unit'])}",
    ]) + "\n"


def _launch_one(hub, cfg, job, rows, avail, hostmap, launched):
    """Place one queued job and launch it, or leave it queued with the reason."""
    job["cpus"], job["placer"], job["board_only"], job["ram_warn"] = None, None, None, None
    if not job["gpu_mib"] and avail["cpu_run"]:
        advice, note = placers.cpu_advise(cfg, job, hostmap)
        if advice:
            pinned = dict(job, host_pref=advice["host"])
            h, gpu, why = place(hub, cfg, pinned, rows)
            if h:
                hdoc = json.loads(rows[h["name"]]["doc"])
                cap = hosts.capacity(h, hdoc)
                cpus = disjoint_cpus(hub, h["name"], cap["threads"], advice["cpus"], job["threads"], hdoc)
                job["cpus"], job["placer"] = json.dumps(cpus), "cpu-run"
            else:  # cpu-run and the inventory disagree: the inventory wins, without the pin
                h, gpu, why = place(hub, cfg, job, rows)
        else:
            h, gpu, why = place(hub, cfg, job, rows)
            if h and note:
                job["placer"] = "hub (" + note + ")"
    else:
        h, gpu, why = place(hub, cfg, job, rows, board=avail["gpu_workboard"] and job.get("adopted", 0) == 0)
    if not h:
        _set(hub, job["id"], reason=why)
        return
    if gpu and avail["gpu_workboard"] and job.get("adopted", 0) == 0:
        if not job.get("board_id"):
            # The inventory chooses a host; the board chooses the card at admission.
            # Pinning this snapshot's UUID strands queued work when another card frees up; only a card the
            # job asked for (--gpu-uuid) goes through, unchanged, and the board's lanes still admit it.
            bid, why = placers.wb_submit(h, cfg, job, job.get("gpu_pin"))
            if not bid and "no gpu-workboard on this host" in (why or "") and job["board_only"]:
                _set(hub, job["id"], reason=job["board_only"])  # the card is full and no board can make room
                return
            if not bid and "no gpu-workboard on this host" in (why or ""):
                job["placer"] = "hub (no gpu-workboard on host)"  # the board is not installed there: hub placement
            elif not bid:
                _set(hub, job["id"], reason=why)
                return
            else:
                job["board_id"] = bid
                _set(hub, job["id"], board_id=bid, placer="gpu-workboard")
        if job.get("board_id"):
            ok, assigned, why = placers.wb_acquire(h, cfg, job["board_id"])
            if not ok:
                _set(hub, job["id"], reason=why)
                return
            if job.get("gpu_pin") and assigned != job["gpu_pin"]:
                # fail closed: a pinned job never runs on a substitute card, and is not retried
                why = f"board assigned {assigned}, not the pinned card {job['gpu_pin']}: not started"
                placers.wb_finish(h, cfg, job["board_id"], "released", why)
                _set(hub, job["id"], state="failed", reason=why, finished_at=now(), collect=1)
                return
            if assigned != gpu["uuid"]:  # the lease decides the card, CUDA_VISIBLE_DEVICES and the hold
                cap = hosts.capacity(h, json.loads(rows[h["name"]]["doc"]))
                gpu = next((g for g in cap["gpus"] if g["uuid"] == assigned), {**gpu, "uuid": assigned})
            job["placer"] = "gpu-workboard"
    doc = json.loads(rows[h["name"]]["doc"])
    _set(hub, job["id"], host=h["name"], gpu_uuid=gpu["uuid"] if gpu else None, state="running",
         started_at=now(), last_seen=now(), reason=job.get("ram_warn"), cpus=job["cpus"],
         placer=job["placer"] or "hub")  # held from now so a parallel launch does not double-book
    rc, out, err = hosts.run_on(h, _launch_script(cfg, h, doc, job, gpu), timeout=60)
    props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l and not l.startswith("Running as unit"))
    # only a unit that systemd-run created can be refused by its HALT condition: `systemctl show` on a unit that
    # does not exist also answers ConditionResult=no, which used to turn every failed launch into a false HALT
    started = "launched" in out.splitlines()
    if "refused HALT" in out or (started and props.get("ConditionResult") == "no"):
        _set(hub, job["id"], state="refused", reason="HALT was raised on the host", finished_at=now(), collect=1)
    elif rc != 0 or not started:
        msg = [l for l in (err + "\n" + out).splitlines()
               if l.strip() and "=" not in l and l != "slice written"][-1:] or ["launch failed"]
        _set(hub, job["id"], state="failed", reason=f"launch failed: {msg[0][:300]}", finished_at=now(), collect=1)
    else:
        pid = int(props["MainPID"]) if props.get("MainPID", "0").isdigit() and props["MainPID"] != "0" else None
        _set(hub, job["id"], pid=pid, invocation=props.get("InvocationID") or None)
        if job.get("board_id"):
            placers.wb_start(h, cfg, job["board_id"], job["unit"], pid)
        launched.append(job["id"])
    if job.get("board_id") and hub.s.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] in FINAL:
        placers.wb_finish(h, cfg, job["board_id"], "released", "did not start")


def launch_queued(hub, cfg=None, only=None):
    """Place and launch queued jobs, oldest first. Nothing launches on a host while a HALT covers it."""
    cfg = cfg or hosts.load_config()
    halted = hosts.halted_hosts(hub, cfg)
    if halted and not [h for h in cfg["hosts"] if h["name"] not in halted]:
        return []
    cfg = {**cfg, "halted": halted}
    rows = {r["name"]: r for r in hub.s.q("SELECT * FROM hosts")}
    q = "SELECT * FROM jobs WHERE state='queued'" + (" AND id=?" if only else "") + " ORDER BY created_at"
    launched = []
    avail = placers.available(cfg)
    hostmap = placers.cpu_run_hostmap(cfg)
    queue = [dict(r) for r in hub.s.q(q, (only,) if only else ())]
    # priority projects in their order, then tiny jobs, then oldest first
    queue.sort(key=lambda r: (tier(cfg, r.get("project")), cuts_in(cfg, r) is None, r["created_at"]))
    for snapshot in [r["id"] for r in queue]:
        with _launching_lock:  # the tick and sb run both land here; one job has one launcher at a time
            if snapshot in _launching:
                continue
            _launching.add(snapshot)
        try:
            # read the row again here: this loop runs ssh per job, so a row read at the top is stale by now, and a
            # stale board_id None submitted the job twice, leaving a lease nobody acquires at the head of the board
            row = hub.s.one("SELECT * FROM jobs WHERE id=?", (snapshot,))
            if row and row["state"] == "queued":
                _launch_one(hub, cfg, dict(row), rows, avail, hostmap, launched)
        finally:
            with _launching_lock:
                _launching.discard(snapshot)
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
    return {"jobs": _seen(hub, [view(dict(r)) for r in hub.s.q(q, tuple(args))])}


def _seen(hub, views):
    """Add to each running job the CPUs its processes are really on (the host's last probe), which is not always
    where it was told to run: a command can pin itself elsewhere."""
    docs = {}
    for v in views:
        if v.get("state") != "running" or not v.get("host") or not v.get("unit"):
            continue
        if v["host"] not in docs:
            r = hub.s.one("SELECT doc FROM hosts WHERE name=?", (v["host"],))
            docs[v["host"]] = json.loads(r["doc"]) if r and r["doc"] else {}
        pin = next((p for p in docs[v["host"]].get("pins") or [] if p.get("unit") == v["unit"]), None)
        v["cpus_seen"] = pin["cpus"] if pin else None
        v["cpus_enforced"] = bool(docs[v["host"]].get("cpuset")) if v.get("cpus") else None
    return views


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


def unplaced(hub):
    """Queued jobs no host has taken yet: for_host never returns them, and the Fleet panel still has to show and stop them."""
    return [view(dict(r)) for r in hub.s.q("SELECT * FROM jobs WHERE host IS NULL AND state='queued' ORDER BY created_at")]


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
