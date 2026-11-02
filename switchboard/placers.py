"""Optional placement backends (docs/FLEET.md, section 4). Detected by the presence of their command; the hub's own
admission applies without them, and the public repo ships with neither.

- gpu-workboard (Atlas GPU workboard): the authority on VRAM reserves, thermals and checkpoint preemption. For a
  GPU job the hub submits and acquires a lease on the target host (the board insists on that), launches with the
  card the board assigned, registers the unit with `start`, heartbeats every probe tick and `finish`es with the
  outcome. A refusal leaves the job queued with the board's reason; a preemption becomes the job state `preempted`.
- cpu-run (fleet_god/cpu_run.py): a placement advisor for CPU jobs. Its dry run names a host and a disjoint CPU
  set after looking at live load and existing reservations; the hub pins the unit to that set (AllowedCPUs) and
  falls back to its own placement when cpu-run refuses or names a host it does not know.

Configuration (hosts.json "jobs"): "gpu_workboard": "auto" | "/path" | null, "cpu_run": "auto" | "/path" | null.
Each host may carry "cpu_run_name" when cpu-run knows it by another name; the board knows hosts by hostname.
"""
import json
import os
import shlex
import shutil

from . import hosts

WB_NAMES = ("gpu-workboard",)
CPU_RUN_NAMES = ("cpu-run",)


def _resolve(setting, names):
    if setting in (None, False, "", "off", "none"):
        return None
    if setting and setting != "auto":
        return setting if os.path.exists(os.path.expanduser(setting)) else None
    for n in names:
        p = shutil.which(n) or (os.path.expanduser(f"~/.local/bin/{n}") if os.path.exists(os.path.expanduser(f"~/.local/bin/{n}")) else None)
        if p:
            return p
    return None


def available(cfg):
    j = cfg.get("jobs") or {}
    return {"gpu_workboard": _resolve(j.get("gpu_workboard", "auto"), WB_NAMES),
            "cpu_run": _resolve(j.get("cpu_run", "auto"), CPU_RUN_NAMES)}


# ---------- gpu-workboard ----------
def _wb_cmd(cfg):
    """The board command as the target host will run it: an explicit path is used as is; auto means PATH there."""
    j = cfg.get("jobs") or {}
    setting = j.get("gpu_workboard", "auto")
    if setting in (None, False, "", "off", "none"):
        return None
    if setting and setting != "auto":
        return shlex.quote(os.path.expanduser(setting))
    return '"$(command -v gpu-workboard || echo "$HOME/.local/bin/gpu-workboard")"'


def _wb(h, cfg, args, timeout=60):
    """Run one board command on a host. Returns (rc, parsed json or None, raw text)."""
    cmd = _wb_cmd(cfg)
    if not cmd:
        return 127, None, "gpu-workboard disabled"
    script = ("set -u\nWB=" + cmd + "\n[ -x \"$WB\" ] || { echo '{\"error\": \"no gpu-workboard on this host\"}'; exit 127; }\n"
              "\"$WB\" " + " ".join(shlex.quote(a) for a in args) + "\n")
    rc, out, err = hosts.run_on(h, script, timeout=timeout)
    data = None
    text = out.strip()
    if text:
        try:
            data = json.loads(text[text.index("{"):]) if "{" in text else None
        except ValueError:
            data = None
    return rc, data, (text or err.strip())[-600:]


def wb_submit(h, cfg, job, gpu_uuid):
    args = ["submit", "--project", job.get("project") or "switchboard", "--label", job["name"][:60],
            "--vram-gib", f"{job['gpu_mib'] / 1024:.2f}", "--duration-min", str(max(1, int(job["hours"] * 60))),
            "--lane", job.get("lane") or "batch", "--gpu", gpu_uuid or "any", "--json",
            "--note", f"switchboard job {job['id']} ({job['agent']})"]
    if job.get("protected"):
        args.append("--protected")
    rc, data, text = _wb(h, cfg, args)
    if rc != 0 or not data:
        return None, f"GPU workboard submit failed: {(data or {}).get('error') or text or f'exit {rc}'}"
    bid = data.get("id") or (data.get("job") or {}).get("id")
    if not bid:
        return None, f"GPU workboard submit returned no id: {text[:200]}"
    return bid, None


def wb_acquire(h, cfg, board_id):
    """(acquired, assigned_uuid, reason). Exit 3 means the board keeps it queued."""
    rc, data, text = _wb(h, cfg, ["acquire", "--json", board_id])
    data = data or {}
    if rc == 0 and data.get("acquired", True):
        return True, data.get("assigned_uuid") or (data.get("job") or {}).get("assigned_uuid"), None
    reason = data.get("decision_reason") or data.get("reason") or data.get("error") or text or f"exit {rc}"
    return False, None, f"GPU workboard: {reason}"


def wb_start(h, cfg, board_id, unit, pid=None):
    args = ["start", board_id, "--unit", unit]
    if pid:
        args += ["--pid", str(pid)]
    rc, data, text = _wb(h, cfg, args)
    return rc == 0, text


def wb_heartbeat(h, cfg, board_id, pid=None, observed_mib=None):
    args = ["heartbeat", board_id]
    if pid:
        args += ["--pid", str(pid)]
    if observed_mib is not None:
        args += ["--observed-mib", str(int(observed_mib))]
    rc, data, text = _wb(h, cfg, args, timeout=40)
    return rc == 0, text


def wb_finish(h, cfg, board_id, outcome, detail=""):
    rc, data, text = _wb(h, cfg, ["finish", board_id, "--outcome", outcome, "--detail", detail[:200]])
    return rc == 0, text


def wb_cancel(h, cfg, board_id, reason=""):
    rc, data, text = _wb(h, cfg, ["cancel", board_id, "--reason", reason[:200]])
    return rc == 0, text


def wb_status(cfg):
    """The board's view, from this machine (the CLI reaches the coordinator itself). None when unavailable."""
    local = next((h for h in cfg["hosts"] if h["ssh"] is None), None)
    if not local or not available(cfg)["gpu_workboard"]:
        return None
    rc, data, text = _wb(local, cfg, ["status", "--json"], timeout=40)
    if rc != 0 or not data:
        return {"available": True, "error": text[:300]}
    cards = [{"host": c.get("host"), "label": c.get("label"), "uuid": c.get("uuid"), "free_mib": c.get("free_mib"),
              "fresh": c.get("fresh"), "active_jobs": c.get("active_job_ids") or []}
             for c in (data.get("fleet") or {}).get("cards") or []]
    jobs = [{"id": j.get("id"), "label": j.get("label"), "project": j.get("project"), "status": j.get("status"),
             "lane": j.get("lane"), "host": j.get("target_host") or j.get("lease_owner_host"),
             "uuid": j.get("assigned_uuid"), "reason": j.get("decision_reason")}
            for j in data.get("jobs") or [] if (j.get("status") or "") not in ("completed", "failed", "released", "cancelled")]
    return {"available": True, "observed_at": data.get("observed_at"), "coordinator": data.get("coordinator"),
            "cards": cards, "jobs": jobs}


def wb_job_status(cfg, board_id):
    st = wb_status(cfg) or {}
    for j in st.get("jobs") or []:
        if j.get("id") == board_id:
            return j
    return None


# ---------- cpu-run ----------
def cpu_advise(cfg, job, hostmap):
    """Ask cpu-run (dry run) where a CPU job should go. Returns {"host": ourname, "cpus": [...]} or None, with a
    reason string for the fleet view when it refused."""
    path = available(cfg)["cpu_run"]
    if not path:
        return None, None
    argv = [path, "--unit", job["unit"].removesuffix(".service"), "--cwd", job["cwd"], "--threads", str(job["threads"]),
            "--memory-mib", str(job["mem_mib"]), "--max-seconds", str(max(60, int(job["hours"] * 3600))), "--dry-run"]
    if job.get("host_pref"):
        name = next((h.get("cpu_run_name") or h["name"] for h in cfg["hosts"] if h["name"] == job["host_pref"]), None)
        if name:
            argv += ["--host", name]
    argv += ["--"] + json.loads(job["argv"])
    local = next(h for h in cfg["hosts"] if h["ssh"] is None)
    script = " ".join(shlex.quote(a) for a in argv) + "\n"
    rc, out, err = hosts.run_on(local, script, timeout=90)
    try:
        data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except (ValueError, IndexError):
        data = {}
    if data.get("status") != "ready":
        why = data.get("reason") or "; ".join(f"{r.get('host')}: {r.get('reason')}" for r in data.get("refusals") or []) \
            or (err.strip()[-200:] if rc != 0 else data.get("status") or "no answer")
        return None, f"cpu-run: {why}"
    ours = hostmap.get(data.get("host"))
    if not ours:
        return None, f"cpu-run chose {data.get('host')}, which is not in hosts.json"
    cpus = [int(c) for c in data.get("cpus") or [] if str(c).isdigit()]
    return {"host": ours, "cpus": cpus}, None


def cpu_run_hostmap(cfg):
    return {(h.get("cpu_run_name") or h["name"]): h["name"] for h in cfg["hosts"]}
