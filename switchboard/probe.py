#!/usr/bin/env python3
"""Switchboard fleet probe: what this host is and what it is doing, as one JSON document on stdout.

Standard library only and self-contained, so the hub runs it on any host as `ssh HOST python3 - < probe.py`
without installing anything there. Every section catches its own errors and reports them under "errors", so a
host with no nvidia-smi, no user manager or no X server still answers. Runs in about a second (the desktop
sample takes one second on a host with an X server).
"""
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time

ERRORS = []


def run(argv, timeout=8):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError) as e:
        ERRORS.append(f"{argv[0]}: {e.__class__.__name__}")
        return None, ""


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def section(fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a probe never dies on one section
        ERRORS.append(f"{fn.__name__}: {e.__class__.__name__}: {e}")
        return default


def identity():
    d = {"hostname": socket.gethostname(), "boot_id": read("/proc/sys/kernel/random/boot_id").strip(),
         "time": time.time(), "uptime_s": float((read("/proc/uptime").split() or ["0"])[0]),
         "wsl": "microsoft" in read("/proc/version").lower(), "python": sys.version.split()[0]}
    rc, out = run(["systemctl", "--version"])
    m = re.search(r"systemd (\d+)", out or "")
    d["systemd"] = int(m.group(1)) if m else None
    rc, out = run(["systemctl", "--user", "is-system-running"])
    d["user_manager"] = (out or "").strip() or None
    return d


def cpu():
    threads = os.cpu_count() or 1
    siblings = set()
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology/thread_siblings_list"):
        s = read(p).strip()
        if s:
            siblings.add(s)
    load = read("/proc/loadavg").split()[:3]
    m = re.search(r"model name\s*:\s*(.+)", read("/proc/cpuinfo"))
    psi = {}
    for k in ("cpu", "memory", "io"):
        t = read(f"/proc/pressure/{k}")
        some = re.search(r"some avg10=([\d.]+)", t)
        full = re.search(r"full avg10=([\d.]+)", t)
        if some:
            psi[k] = {"some10": float(some.group(1)), "full10": float(full.group(1)) if full else None}
    return {"threads": threads, "cores": len(siblings) or threads, "model": m.group(1).strip() if m else None,
            "load": [float(x) for x in load] if len(load) == 3 else None, "psi": psi or None}


def memory():
    mi = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            mi[k] = int(parts[0]) // 1024  # MiB
    swap_total = mi.get("SwapTotal")
    return {"total_mib": mi.get("MemTotal"), "available_mib": mi.get("MemAvailable"), "swap_total_mib": swap_total,
            "swap_used_mib": (swap_total - mi.get("SwapFree", 0)) if swap_total is not None else None}


def _num(s):
    try:
        return int(float(s))
    except ValueError:
        return None


def gpus():
    rc, out = run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,"
                   "temperature.gpu,display_active", "--format=csv,noheader,nounits"], 12)
    if rc != 0 or not (out or "").strip():
        return []
    cards = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 8:
            continue
        cards.append({"index": _num(f[0]), "uuid": f[1], "name": f[2], "vram_total_mib": _num(f[3]),
                      "vram_used_mib": _num(f[4]), "util": _num(f[5]), "temp_c": _num(f[6]),
                      "display": f[7].lower() == "enabled", "procs": []})
    by_uuid = {c["uuid"]: c for c in cards}
    rc, out = run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                   "--format=csv,noheader,nounits"], 12)
    for line in (out or "").strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) >= 4 and f[0] in by_uuid:
            by_uuid[f[0]]["procs"].append({"pid": _num(f[1]), "name": os.path.basename(f[2])[:60],
                                           "vram_mib": _num(f[3])})
    return cards


UNIT_RE = re.compile(r"/([^/]+\.(?:service|scope))(?:/|$)")


def unit_of(pid):
    cg = read(f"/proc/{pid}/cgroup")
    hits = UNIT_RE.findall(cg)
    return hits[-1] if hits else None


def processes(n=15):
    rc, out = run(["ps", "-eo", "pid,ppid,pcpu,rss,etimes,comm,args", "--sort=-pcpu"], 10)
    rows = []
    for line in (out or "").splitlines()[1:]:
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, pcpu, rss, etimes, comm, args = parts
        if comm == "ps":
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = None
        rows.append({"pid": int(pid), "ppid": int(ppid), "cpu": float(pcpu), "rss_mib": int(rss) // 1024,
                     "age_s": int(etimes), "comm": comm, "args": args[:160], "unit": unit_of(pid), "cwd": cwd,
                     "ancestors": ancestors(int(ppid))})
        if len(rows) >= n:
            break
    return rows


def ancestors(pid, limit=16):
    """Parent chain of a process (nearest first), so the hub can tell which agent terminal launched it."""
    chain = []
    while pid > 1 and len(chain) < limit:
        chain.append(pid)
        st = read(f"/proc/{pid}/stat")
        m = re.search(r"\)\s+\S\s+(\d+)", st)
        if not m:
            break
        pid = int(m.group(1))
    return chain


def _show(names, props):
    """systemctl --user show for several units: a list of property dicts."""
    if not names:
        return []
    rc, out = run(["systemctl", "--user", "show", "-p", ",".join(props), "--"] + names, 15)
    blocks, cur = [], {}
    for line in (out or "").splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        blocks.append(cur)
    return blocks


def _names(args):
    rc, out = run(["systemctl", "--user"] + args + ["--plain", "--no-legend"], 10)
    return [l.split()[0] for l in (out or "").splitlines() if l.strip()]


UNIT_PROPS = ["Id", "Description", "ActiveState", "SubState", "Result", "MainPID", "CPUUsageNSec", "MemoryCurrent",
              "InvocationID", "ActiveEnterTimestamp", "ExecMainStatus", "ConditionResult"]


def _unit_row(b):
    def num(k):
        v = b.get(k, "")
        return int(v) if v.isdigit() else None
    cpu_ns = num("CPUUsageNSec")
    return {"unit": b.get("Id"), "description": (b.get("Description") or "")[:120], "active": b.get("ActiveState"),
            "sub": b.get("SubState"), "result": b.get("Result"), "pid": num("MainPID") or None,
            "cpu_s": round(cpu_ns / 1e9, 1) if cpu_ns is not None else None,
            "mem_mib": (num("MemoryCurrent") or 0) // (1024 * 1024) if num("MemoryCurrent") is not None else None,
            "invocation": b.get("InvocationID") or None, "since": b.get("ActiveEnterTimestamp") or None,
            "exit": num("ExecMainStatus"), "condition": b.get("ConditionResult") or None}


def units():
    names = _names(["list-units", "--type=service", "--state=running"])
    return [_unit_row(b) for b in _show(names, UNIT_PROPS)]


def jobs():
    names = _names(["list-units", "sb-job-*", "--all"])
    return [_unit_row(b) for b in _show(names, UNIT_PROPS)]


def timers():
    rc, out = run(["systemctl", "--user", "list-timers", "--all", "--plain", "--no-legend"], 10)
    rows = []
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-2].endswith(".timer"):
            rows.append({"timer": parts[-2], "activates": parts[-1]})
    return rows


def unit_files():
    """Unit files in the user's own directory: the ones a halt guard can be installed on."""
    d = os.path.expanduser(os.environ.get("SB_SYSTEMD_USER_DIR", "~/.config/systemd/user"))
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.service")) + glob.glob(os.path.join(d, "*.timer"))):
        if os.path.islink(p) and not os.path.exists(p):
            continue
        name = os.path.basename(p)
        out.append({"unit": name, "guarded": os.path.exists(os.path.join(d, name + ".d", "switchboard-halt.conf"))})
    return out


def desktop():
    """CPU share and wakeups per second of the X server, the honest measure of input lag on a desktop host."""
    rc, out = run(["ps", "-eo", "pid,comm"], 5)
    pids = [int(l.split()[0]) for l in (out or "").splitlines()[1:]
            if len(l.split()) == 2 and l.split()[1] in ("Xorg", "X", "Xwayland")]
    if not pids:
        return None
    pid = pids[0]

    def sample():
        st = read(f"/proc/{pid}/stat").split()
        vol = re.search(r"voluntary_ctxt_switches:\s+(\d+)", read(f"/proc/{pid}/status"))
        return (int(st[13]) + int(st[14])) if len(st) > 14 else 0, int(vol.group(1)) if vol else 0

    c1, w1 = sample()
    time.sleep(1.0)
    c2, w2 = sample()
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    return {"pid": pid, "xorg_cpu_pct": round((c2 - c1) * 100.0 / hz, 1), "xorg_wakeups_s": w2 - w1,
            "display": os.environ.get("DISPLAY") or None}


def main():
    doc = {"probe": 1,
           "identity": section(identity, {}),
           "cpu": section(cpu, {}),
           "memory": section(memory, {}),
           "gpus": section(gpus, []),
           "processes": section(processes, []),
           "units": section(units, []),
           "jobs": section(jobs, []),
           "timers": section(timers, []),
           "unit_files": section(unit_files, []),
           "desktop": section(desktop, None)}
    doc["errors"] = ERRORS
    doc["took_ms"] = int((time.time() - doc["identity"].get("time", time.time())) * 1000)
    sys.stdout.write(json.dumps(doc, separators=(",", ":")))


if __name__ == "__main__":
    main()
