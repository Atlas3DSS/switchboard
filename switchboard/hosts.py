"""Fleet inventory and halt (docs/FLEET.md, phase 1).

Hosts come from ~/.config/switchboard/hosts.json (SB_HOSTS): a name, an ssh alias from the user's own ssh config
(null for this machine), whether the owner's display is on it, a placement order and a reserve. The local machine
is always a host, so a fresh install has a fleet of one. Nothing else is declared: every 30 s the hub runs
probe.py on each host (locally, or `ssh ALIAS python3 -` with the script on stdin) and keeps the answer.

Halt writes $STATE/HALT on every host and stops the tracked `sb-job-*` units; with --workloads it also disables
every unit matching `workload_units` that is not protected. Guards are one-line systemd drop-ins
(`ConditionPathExists=!%h/.local/state/switchboard/HALT`) so a timer, a Restart= loop or a typed `systemctl start`
cannot start a workload while the flag exists.
"""
import fnmatch
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .store import now

CONFIG = os.path.expanduser(os.environ.get("SB_HOSTS", "~/.config/switchboard/hosts.json"))  # default; read again per call


def config_path():
    return os.path.expanduser(os.environ.get("SB_HOSTS", CONFIG))


def ssh_bin():
    return os.environ.get("SB_SSH", "ssh")
PROBE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
PROBE_S = float(os.environ.get("SB_FLEET_PROBE_S", "30"))
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2"]
HALT_REL = ".local/state/switchboard/HALT"  # under $HOME on every host; the drop-ins name it with %h
SAMPLES_KEPT = 120
PATTERN_OK = re.compile(r"^[A-Za-z0-9_.@*?\[\]-]+$")
DEFAULT_JOBS = {"slice_cpu_weight": 20, "untracked_policy": "flag", "display_gpu": "avoid",
                "gpu_workboard": "auto", "cpu_run": "auto"}
DEFAULT_RESERVE = {"cores": 1, "ram_gib": 8}
# A desktop host keeps its display GL clients visible but they are never "untracked work".
DESKTOP_COMMS = {"Xorg", "X", "Xwayland", "picom", "compton", "gnome-shell", "kwin_x11", "kwin_wayland", "awesome"}

# The tables live in store.SCHEMA; kept here as documentation of what the inventory stores.
SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
  name TEXT PRIMARY KEY, cfg TEXT, doc TEXT, probed_at TEXT, ok INTEGER DEFAULT 0, error TEXT, ms INTEGER);
CREATE TABLE IF NOT EXISTS host_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT, ts TEXT, load1 REAL, available_mib INTEGER, gpus TEXT, desktop TEXT);
CREATE INDEX IF NOT EXISTS host_samples_host ON host_samples(host, id);
"""


# ---------- config ----------
def local_name():
    return re.sub(r"[^a-z0-9]+", "-", socket.gethostname().lower()).strip("-") or "local"


def load_config(path=None):
    """The hosts file with defaults filled in; the local machine is always present."""
    path = path or config_path()
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f) or {}
    hosts = []
    for i, h in enumerate(cfg.get("hosts") or []):
        if not isinstance(h, dict) or not h.get("name"):
            raise ValueError(f"hosts[{i}] needs a name")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", h["name"]):
            raise ValueError(f"host name {h['name']!r}: letters, digits, . _ - only")
        hosts.append({"name": h["name"], "ssh": h.get("ssh") or None, "desktop": bool(h.get("desktop", False)),
                      "order": int(h.get("order", 100)), "reserve": {**DEFAULT_RESERVE, **(h.get("reserve") or {})},
                      "labels": list(h.get("labels") or []), "cpu_run_name": h.get("cpu_run_name") or None})
    if not any(h["ssh"] is None for h in hosts):
        hosts.insert(0, {"name": local_name(), "ssh": None, "desktop": bool(os.environ.get("DISPLAY")),
                         "order": 100, "reserve": dict(DEFAULT_RESERVE), "labels": []})
    hosts.sort(key=lambda h: (h["order"], h["name"]))
    for key in ("protected_units", "workload_units"):
        for pat in cfg.get(key) or []:
            if not isinstance(pat, str) or not PATTERN_OK.match(pat):
                raise ValueError(f"{key}: {pat!r} is not a unit glob")
    budgets = {}
    for proj, b in (cfg.get("budgets") or {}).items():
        if not isinstance(b, dict):
            raise ValueError(f"budgets.{proj} must be an object")
        budgets[proj] = {k: float(b[k]) for k in ("threads", "mem_gib", "gpu_gib", "jobs") if b.get(k) is not None}
    return {"path": path, "exists": os.path.exists(path), "hosts": hosts, "budgets": budgets,
            "jobs": {**DEFAULT_JOBS, **(cfg.get("jobs") or {})},
            "protected_units": list(cfg.get("protected_units") or []),
            "workload_units": list(cfg.get("workload_units") or [])}


def save_host(entry, path=None):
    """Add or replace one host in the file (sb fleet add-host). Keeps everything else in the file as it is."""
    path = path or config_path()
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f) or {}
    hosts = [h for h in (cfg.get("hosts") or []) if h.get("name") != entry["name"]]
    hosts.append(entry)
    cfg["hosts"] = hosts
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return load_config(path)


def host(cfg, name):
    for h in cfg["hosts"]:
        if h["name"] == name:
            return h
    raise LookupError(f"no host named {name}")


# ---------- running things on a host ----------
def run_on(h, script, timeout=30, interpreter="bash -s"):
    """Run a script on the host: locally, or over its ssh alias with the script on stdin. (rc, stdout, stderr)."""
    if h["ssh"] is None:
        argv = shlex.split(interpreter)
        if argv[:2] == ["python3", "-"]:
            argv = [sys.executable, "-"]
    else:
        argv = [ssh_bin()] + SSH_OPTS + [h["ssh"], interpreter]
    try:
        p = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as e:
        return 127, "", str(e)


def probe(h, timeout=40):
    with open(PROBE_PATH) as f:
        src = f.read()
    t = time.time()
    rc, out, err = run_on(h, src, timeout=timeout, interpreter="python3 -")
    ms = int((time.time() - t) * 1000)
    if rc != 0:
        return None, (err.strip().splitlines() or [f"exit {rc}"])[-1][:300], ms
    try:
        return json.loads(out), None, ms
    except ValueError:
        return None, "probe returned no JSON" + (f": {err.strip()[:200]}" if err.strip() else ""), ms


# ---------- the loop ----------
def tick(hub, cfg=None):
    """Probe every host (in parallel) and store the answers. Returns the per-host outcome."""
    cfg = cfg or load_config()
    names = {h["name"] for h in cfg["hosts"]}
    for row in hub.s.q("SELECT name FROM hosts"):
        if row["name"] not in names:
            hub.s.x("DELETE FROM hosts WHERE name=?", (row["name"],))
    with ThreadPoolExecutor(max_workers=max(1, len(cfg["hosts"]))) as ex:
        results = list(ex.map(lambda h: (h, *probe(h)), cfg["hosts"]))
    out = []
    for h, doc, err, ms in results:
        ts = now()
        prev = hub.s.one("SELECT doc, probed_at FROM hosts WHERE name=?", (h["name"],))
        if doc is not None:
            if prev and prev["doc"]:
                doc["rates"] = _rates(json.loads(prev["doc"]), doc)
            hub.s.x("INSERT INTO hosts(name,cfg,doc,probed_at,ok,error,ms) VALUES(?,?,?,?,1,NULL,?) "
                    "ON CONFLICT(name) DO UPDATE SET cfg=excluded.cfg, doc=excluded.doc, probed_at=excluded.probed_at,"
                    " ok=1, error=NULL, ms=excluded.ms", (h["name"], json.dumps(h), json.dumps(doc), ts, ms))
            hub.s.x("INSERT INTO host_samples(host,ts,load1,available_mib,gpus,desktop) VALUES(?,?,?,?,?,?)",
                    (h["name"], ts, (doc.get("cpu") or {}).get("load", [None])[0],
                     (doc.get("memory") or {}).get("available_mib"),
                     json.dumps([{"uuid": g["uuid"], "used": g["vram_used_mib"], "util": g["util"]}
                                 for g in doc.get("gpus") or []]),
                     json.dumps(doc.get("desktop"))))
            hub.s.x("DELETE FROM host_samples WHERE host=? AND id NOT IN (SELECT id FROM host_samples WHERE host=? "
                    "ORDER BY id DESC LIMIT ?)", (h["name"], h["name"], SAMPLES_KEPT))
        else:
            # keep the last good document; record the failure beside it
            hub.s.x("INSERT INTO hosts(name,cfg,doc,probed_at,ok,error,ms) VALUES(?,?,NULL,NULL,0,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET cfg=excluded.cfg, ok=0, error=excluded.error, ms=excluded.ms",
                    (h["name"], json.dumps(h), err, ms))
        out.append({"host": h["name"], "ok": doc is not None, "error": err, "ms": ms})
    return out


def _rates(prev, cur):
    """Per-unit CPU rate between two probes, from cumulative CPUUsageNSec."""
    dt = (cur.get("identity") or {}).get("time", 0) - (prev.get("identity") or {}).get("time", 0)
    if dt <= 0:
        return {}
    before = {u["unit"]: u for u in (prev.get("units") or []) + (prev.get("jobs") or []) if u.get("cpu_s") is not None}
    rates = {}
    for u in (cur.get("units") or []) + (cur.get("jobs") or []):
        b = before.get(u.get("unit"))
        if b and u.get("cpu_s") is not None and u.get("invocation") == b.get("invocation"):
            rates[u["unit"]] = round(max(0.0, (u["cpu_s"] - b["cpu_s"]) / dt * 100), 1)
    return {"units_cpu_pct": rates}


# ---------- views ----------
def capacity(h, doc):
    cpu, mem = doc.get("cpu") or {}, doc.get("memory") or {}
    threads, cores = cpu.get("threads") or 0, cpu.get("cores") or 0
    per_core = (threads // cores) if cores else 1
    load1 = (cpu.get("load") or [0])[0] or 0
    reserve_threads = h["reserve"]["cores"] * per_core
    free_threads = max(0, int(threads - reserve_threads - round(load1)))
    avail = mem.get("available_mib")
    free_ram_gib = None if avail is None else round(max(0, avail / 1024 - h["reserve"]["ram_gib"]), 1)
    gpus = []
    for g in doc.get("gpus") or []:
        free = None if g.get("vram_total_mib") is None else g["vram_total_mib"] - (g.get("vram_used_mib") or 0)
        gpus.append({"uuid": g["uuid"], "name": g["name"], "index": g.get("index"), "display": g.get("display"),
                     "util": g.get("util"), "temp_c": g.get("temp_c"), "vram_total_mib": g.get("vram_total_mib"),
                     "vram_used_mib": g.get("vram_used_mib"), "vram_free_mib": free,
                     "procs": g.get("procs") or []})
    return {"threads": threads, "cores": cores, "load": cpu.get("load"), "free_threads": free_threads,
            "ram_total_gib": None if mem.get("total_mib") is None else round(mem["total_mib"] / 1024, 1),
            "ram_available_gib": None if avail is None else round(avail / 1024, 1), "free_ram_gib": free_ram_gib,
            "swap_used_mib": mem.get("swap_used_mib"), "swap_total_mib": mem.get("swap_total_mib"),
            "psi": cpu.get("psi"), "gpus": gpus}


def _matches(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def untracked(cfg, doc, min_cpu=25.0, agent_pids=None, tracked_units=()):
    """Heavy processes outside a tracked job or a protected unit, attributed: the agent terminal they descend
    from (this machine only), else their unit, else their working directory."""
    out = []
    agent_pids = agent_pids or {}
    for p in doc.get("processes") or []:
        if p.get("cpu", 0) < min_cpu or p.get("comm") in DESKTOP_COMMS:
            continue
        unit = p.get("unit") or ""
        if unit.startswith("sb-job-") or unit in tracked_units or (unit and _matches(unit, cfg["protected_units"])):
            continue
        if p.get("pid") in agent_pids or any(a in agent_pids for a in p.get("ancestors") or []):
            if p.get("comm") in ("claude", "codex", "node", "gemini", "agy", "hermes") and not unit:
                continue  # the agent CLI itself, not work it launched
            who = agent_pids.get(p.get("pid")) or next(agent_pids[a] for a in p["ancestors"] if a in agent_pids)
            attribution, agent = f"agent {who} (terminal)", who
        elif unit:
            attribution, agent = unit, None
        elif p.get("cwd"):
            attribution, agent = "cwd " + p["cwd"], None
        else:
            attribution, agent = "unknown", None
        out.append({**{k: p.get(k) for k in ("pid", "ppid", "cpu", "rss_mib", "age_s", "comm", "args", "unit", "cwd")},
                    "attribution": attribution, "agent": agent})
    return out


def host_view(cfg, h, row, agent_pids=None, tracked=None):
    doc = json.loads(row["doc"]) if row and row["doc"] else None
    tracked = tracked or []
    age = None
    if row and row["probed_at"]:
        from .store import epoch
        age = int(time.time() - epoch(row["probed_at"]))
    v = {"name": h["name"], "ssh": h["ssh"], "desktop": h["desktop"], "order": h["order"], "reserve": h["reserve"],
         "labels": h["labels"], "reachable": bool(row and row["ok"]), "error": row["error"] if row else "not probed yet",
         "probed_at": row["probed_at"] if row else None, "age_s": age, "probe_ms": row["ms"] if row else None}
    if doc:
        ident = doc.get("identity") or {}
        v.update({"hostname": ident.get("hostname"), "systemd": ident.get("systemd"), "wsl": ident.get("wsl"),
                  "user_manager": ident.get("user_manager"), "uptime_s": ident.get("uptime_s"),
                  "capacity": capacity(h, doc), "desktop_metrics": doc.get("desktop"),
                  "jobs": doc.get("jobs") or [], "units_running": len(doc.get("units") or []),
                  "timers": len(doc.get("timers") or []),
                  "guards": sum(1 for u in doc.get("unit_files") or [] if u.get("guarded")),
                  "unit_files": len(doc.get("unit_files") or []),
                  "top": [{k: p.get(k) for k in ("pid", "cpu", "rss_mib", "comm", "unit", "age_s")}
                          for p in (doc.get("processes") or [])[:8]],
                  "untracked": untracked(cfg, doc, agent_pids=agent_pids if h["ssh"] is None else None,
                                         tracked_units={j["unit"] for j in tracked}),
                  "tracked": tracked, "errors": doc.get("errors") or []})
    return v


def fleet_view(hub):
    from . import jobs
    cfg = load_config()
    rows = {r["name"]: r for r in hub.s.q("SELECT * FROM hosts")}
    agent_pids = jobs.terminal_pids(hub)
    return {"ts": now(), "config": {"path": cfg["path"], "exists": cfg["exists"], "jobs": cfg["jobs"],
                                    "budgets": cfg["budgets"], "protected_units": cfg["protected_units"],
                                    "workload_units": cfg["workload_units"]},
            "probe_s": PROBE_S, "halt": halt_state(hub), "placers": jobs.placers_view(hub, cfg),
            "hosts": [host_view(cfg, h, rows.get(h["name"]), agent_pids, jobs.for_host(hub, h["name"])) for h in cfg["hosts"]]}


def summary(hub):
    """The compact form the dashboard polls with /api/overview."""
    try:
        v = fleet_view(hub)
    except (ValueError, OSError) as e:
        return {"error": str(e), "hosts": [], "halt": halt_state(hub)}
    out = []
    for h in v["hosts"]:
        c = h.get("capacity") or {}
        out.append({"name": h["name"], "desktop": h["desktop"], "reachable": h["reachable"], "age_s": h["age_s"],
                    "error": h["error"], "load1": (c.get("load") or [None])[0], "threads": c.get("threads"),
                    "free_threads": c.get("free_threads"), "free_ram_gib": c.get("free_ram_gib"),
                    "ram_total_gib": c.get("ram_total_gib"),
                    "gpus": [{"name": g["name"], "free_mib": g["vram_free_mib"], "total_mib": g["vram_total_mib"],
                              "util": g["util"], "display": g["display"]} for g in c.get("gpus") or []],
                    "xorg_cpu_pct": (h.get("desktop_metrics") or {}).get("xorg_cpu_pct"),
                    "xorg_wakeups_s": (h.get("desktop_metrics") or {}).get("xorg_wakeups_s"),
                    "jobs": len(h.get("tracked") or []), "untracked": len(h.get("untracked") or [])})
    return {"hosts": out, "halt": v["halt"], "config_exists": v["config"]["exists"]}


# ---------- halt ----------
def halt_state(hub):
    return hub.s.setting("halt")


def _case_patterns(patterns):
    return "|".join(patterns) if patterns else "__no_pattern__"


def _halt_script(reason_line, workloads, workload_units, protected_units):
    """Bash for one host: raise the flag, stop tracked jobs, optionally disable matching workload units/timers."""
    lines = [
        "set -u",
        'S="${SB_STATE:-$HOME/.local/state/switchboard}"',
        'mkdir -p "$S" && chmod 700 "$S"',
        f'printf %s\\\\n {shlex.quote(reason_line)} > "$S/HALT" && echo "flag $S/HALT"',
        "for u in $(systemctl --user list-units 'sb-job-*' --plain --no-legend --state=running,activating 2>/dev/null "
        "| awk '{print $1}'); do systemctl --user stop \"$u\" 2>/dev/null && echo \"stopped $u\"; done",
    ]
    if workloads:
        prot, work = _case_patterns(protected_units), _case_patterns(workload_units)
        lines += [
            "for u in $(systemctl --user list-units --type=service,timer --plain --no-legend "
            "--state=running,activating,waiting,active 2>/dev/null | awk '{print $1}'); do",
            f"  case \"$u\" in {prot}) continue;; esac",
            f"  case \"$u\" in {work}) systemctl --user disable --now \"$u\" >/dev/null 2>&1 && echo \"disabled $u\" "
            "|| echo \"failed $u\";; esac",
            "done",
        ]
    return "\n".join(lines) + "\n"


def halt(hub, who, reason, hosts=None, workloads=False, cfg=None):
    cfg = cfg or load_config()
    targets = [h for h in cfg["hosts"] if not hosts or h["name"] in hosts]
    if hosts and len(targets) != len(set(hosts)):
        raise LookupError("unknown host: " + ", ".join(sorted(set(hosts) - {h["name"] for h in targets})))
    if workloads and not cfg["workload_units"]:
        raise ValueError("--workloads needs workload_units in " + cfg["path"])
    ts = now()
    line = f"HALT by {who} at {ts}: {reason or 'no reason given'}"
    script = _halt_script(line, workloads, cfg["workload_units"], cfg["protected_units"])
    results = []
    for h in targets:
        rc, out, err = run_on(h, script, timeout=90)
        acts = [l for l in out.splitlines() if l.strip()]
        results.append({"host": h["name"], "ok": rc == 0 and any(l.startswith("flag ") for l in acts),
                        "flag": any(l.startswith("flag ") for l in acts),
                        "stopped": [l[8:] for l in acts if l.startswith("stopped ")],
                        "disabled": [l[9:] for l in acts if l.startswith("disabled ")],
                        "failed": [l[7:] for l in acts if l.startswith("failed ")],
                        "error": (err.strip()[-300:] or None) if rc != 0 else None})
    state = {"by": who, "reason": reason or "", "at": ts, "hosts": [r["host"] for r in results if r["ok"]],
             "pending": [r["host"] for r in results if not r["ok"]], "workloads": bool(workloads)}
    hub.s.set_setting("halt", state)
    return {"halt": state, "results": results}


def resume(hub, who, hosts=None, cfg=None):
    cfg = cfg or load_config()
    targets = [h for h in cfg["hosts"] if not hosts or h["name"] in hosts]
    script = 'S="${SB_STATE:-$HOME/.local/state/switchboard}"; rm -f "$S/HALT" && echo cleared\n'
    results = []
    for h in targets:
        rc, out, err = run_on(h, script, timeout=30)
        results.append({"host": h["name"], "ok": rc == 0 and "cleared" in out, "error": err.strip()[-300:] or None})
    state = halt_state(hub)
    if state:
        left = [n for n in state.get("hosts", []) + state.get("pending", [])
                if n not in {r["host"] for r in results if r["ok"]}]
        if left and hosts:
            state = {**state, "hosts": left, "pending": [], "resumed_by": who}
            hub.s.set_setting("halt", state)
        else:
            hub.s.set_setting("halt", None)
            state = None
    return {"halt": state, "results": results}


def retry_pending_halt(hub):
    """Called from the probe loop: a host that missed the flag gets it when it comes back."""
    state = halt_state(hub)
    if not state or not state.get("pending"):
        return
    cfg = load_config()
    line = f"HALT by {state['by']} at {state['at']}: {state.get('reason') or 'no reason given'}"
    still = []
    for name in state["pending"]:
        try:
            h = host(cfg, name)
        except LookupError:
            continue
        rc, out, err = run_on(h, _halt_script(line, False, [], []), timeout=60)
        (state["hosts"].append(name) if rc == 0 and "flag " in out else still.append(name))
    state["pending"] = still
    hub.s.set_setting("halt", state)


# ---------- guards ----------
GUARD = "[Unit]\nConditionPathExists=!%h/.local/state/switchboard/HALT\n"


def _guards_script(action, workload_units, protected_units):
    prot, work = _case_patterns(protected_units), _case_patterns(workload_units)
    body = {
        "list": 'for d in "$D"/*.d; do [ -f "$d/switchboard-halt.conf" ] && echo "guarded ${d##*/}"; done; true',
        "install": "\n".join([
            'for f in "$D"/*.service "$D"/*.timer; do [ -e "$f" ] || continue; u="${f##*/}"',
            f'  case "$u" in {prot}) continue;; esac',
            f'  case "$u" in {work}) ;; *) continue;; esac',
            '  mkdir -p "$D/$u.d" && printf %s {GUARD} > "$D/$u.d/switchboard-halt.conf" && echo "guarded $u"',
            "done",
            'systemctl --user daemon-reload 2>/dev/null; true']),
        "remove": "\n".join([
            'for d in "$D"/*.d; do f="$d/switchboard-halt.conf"; [ -f "$f" ] || continue',
            '  rm -f "$f" && echo "unguarded ${d##*/}"; rmdir "$d" 2>/dev/null',
            "done",
            'systemctl --user daemon-reload 2>/dev/null; true']),
    }[action].replace("{GUARD}", shlex.quote(GUARD))
    return 'set -u\nD="${SB_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"\n' + body + "\n"


def guards(hub, action, hosts=None, cfg=None):
    """action: list | install | remove. Drop-ins go on units in the user's own unit directory only."""
    cfg = cfg or load_config()
    if action == "install" and not cfg["workload_units"]:
        raise ValueError("installing guards needs workload_units in " + cfg["path"])
    targets = [h for h in cfg["hosts"] if not hosts or h["name"] in hosts]
    results = []
    for h in targets:
        rc, out, err = run_on(h, _guards_script(action, cfg["workload_units"], cfg["protected_units"]), timeout=60)
        units = [l.split(" ", 1)[1].removesuffix(".d") for l in out.splitlines() if " " in l]
        results.append({"host": h["name"], "ok": rc == 0, "units": sorted(set(units)),
                        "error": err.strip()[-300:] or None if rc != 0 else None})
    return {"action": action, "results": results}


# ---------- add-host ----------
def add_host(body):
    name = (body.get("name") or "").strip()
    alias = (body.get("ssh") or "").strip() or None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        raise ValueError("name: letters, digits, . _ - only")
    if alias and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", alias):
        raise ValueError("ssh: an alias or host name from your ssh config")
    entry = {"name": name, "ssh": alias, "desktop": bool(body.get("desktop", False)),
             "order": int(body.get("order", 100))}
    if body.get("reserve"):
        entry["reserve"] = {k: body["reserve"][k] for k in ("cores", "ram_gib") if k in body["reserve"]}
    trial = {**entry, "reserve": {**DEFAULT_RESERVE, **entry.get("reserve", {})}, "labels": []}
    doc, err, ms = probe(trial)
    if doc is None:
        raise ValueError(f"{name}: probe failed ({err}); fix the ssh alias first (BatchMode, key auth)")
    cfg = save_host(entry)
    return {"host": host_view(cfg, host(cfg, name), {"doc": json.dumps(doc), "probed_at": now(), "ok": 1,
                                                     "error": None, "ms": ms}), "config": cfg["path"]}


def wsl_keepalive(distro="Ubuntu"):
    distro = re.sub(r"[^A-Za-z0-9_.-]", "", distro) or "Ubuntu"
    return (f'schtasks /Create /TN "WSL keepalive" /SC ONLOGON /RL LIMITED /F '
            f'/TR "C:\\Windows\\System32\\wsl.exe -d {distro} --exec sleep infinity"\n'
            'schtasks /Run /TN "WSL keepalive"\n'
            "# Then in Task Scheduler: open the task, tick Hidden and \"Run whether user is logged on or not\".\n"
            "# Why: a WSL instance stops when its last console closes and takes sshd and every tunnel with it.")
