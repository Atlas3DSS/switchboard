# Fleet: what Switchboard has, what runs on it, and the one way to launch

Agents launch work. Left to themselves they launch it by hand, on whatever machine they are on, with `systemd-run`
or a background shell, and nobody can list what is running, who started it, or stop it when the desktop starts to
lag. Switchboard's fleet layer gives the hub three things: an **inventory** of the machines it can reach, a **halt**
that systemd itself honours, and **one launch verb** that places work, bounds it and tracks it.

Everything works on a fleet of one (the machine the hub runs on) with no configuration. Adding machines is one
file.

## 1. Inventory

### Hosts file

`~/.config/switchboard/hosts.json` (`SB_HOSTS` overrides). The local machine is always present even when the file
is missing. A host is a name plus how to reach it, nothing more: everything else is discovered.

```json
{
  "hosts": [
    {"name": "desk",        "ssh": null,          "desktop": true,  "order": 3, "reserve": {"cores": 1, "ram_gib": 8}},
    {"name": "workstation", "ssh": "workstation", "desktop": false, "order": 2, "reserve": {"cores": 1, "ram_gib": 8}},
    {"name": "gpubox",      "ssh": "gpubox",      "desktop": false, "order": 1, "reserve": {"cores": 2, "ram_gib": 16}}
  ],
  "jobs": {"slice_cpu_weight": 20, "untracked_policy": "flag", "display_gpu": "avoid",
           "gpu_workboard": "auto", "cpu_run": "auto"},
  "budgets": {"default": {"jobs": 4}},
  "protected_units": ["switchboard*", "*-tunnel.service", "pipewire*", "dbus*"],
  "workload_units":  ["research-*", "*-scorer.service", "*-worker.service"]
}
```

- `ssh`: an alias from your own `~/.ssh/config` (key auth, `BatchMode` must work), or `null` for this machine.
- `desktop`: the host whose display you sit at. Its jobs slice gets a low CPU weight so a runaway job still yields
  to a keystroke, and its X server's CPU share and wakeups are reported as the honest measure of input lag.
- `order`: placement preference, 1 first. `reserve`: a core (and its siblings) and RAM never given to a job.
- `protected_units` / `workload_units`: unit-name globs, used only by `sb halt --workloads` and the guards.

`examples/hosts.example.json` carries this shape. `sb fleet add-host NAME --ssh ALIAS [--desktop] [--order N]`
probes the host first and writes it in only if it answers.

### Probe

`switchboard/probe.py` is a standard-library script the hub runs locally and, on remote hosts, as
`ssh ALIAS python3 -` with the script on stdin, so nothing is installed remotely beyond `python3` (and `nvidia-smi`
for GPUs). It returns identity (hostname, systemd version, WSL), CPU topology and load, memory and swap, GPUs with
their display flag and compute processes, the top processes with their systemd unit and parent chain, running user
units with CPU time, `sb-job-*` units, timers, unit files, and on a desktop host the X server's CPU share and
wakeups per second. It answers in about a second.

The hub probes every host every 30 s (`SB_FLEET_PROBE_S`) and keeps the latest document plus a short history. A
host that stops answering keeps its last document and is marked down with the error.

```
sb fleet                 one row per host: reachability, load, free threads, free RAM, GPUs (free VRAM, * = display)
sb fleet check           probe every host now
sb fleet show HOST       one host in full
GET /api/fleet           the same, as JSON; /api/overview carries a compact "fleet" summary for the dashboard
```

## 2. Halt

```
sb halt [--reason "..."] [--host H] [--workloads]
sb unhalt [--host H]
sb halt --install-guards | --list-guards | --remove-guards
```

`sb halt` writes `~/.local/state/switchboard/HALT` on every reachable host (hosts that are down get it when they
answer again) and stops every tracked job. With `--workloads` it also runs `systemctl --user disable --now` on every
unit and timer matching `workload_units` that is not protected, and prints what it touched. While HALT is raised,
`sb run` refuses and the dashboard shows the reason.

A halt that agents must remember to check is no halt. `--install-guards` writes one drop-in per matching unit file:

```
# ~/.config/systemd/user/<unit>.d/switchboard-halt.conf
[Unit]
ConditionPathExists=!%h/.local/state/switchboard/HALT
```

systemd evaluates the condition at every start, so a timer firing, a `Restart=always` loop or a typed
`systemctl start` produces "condition failed", not a running process. `sb unhalt` deletes the file.

## 3. Jobs

```
sb run --name NAME [--host H] [--threads N] [--mem GIB] [--gpu GIB] [--hours H] [--cwd DIR]
       [--lane batch|interactive] [--protected] -- command args...
sb jobs [--all] [--project P] [--host H] [--mine]
sb jobs show ID [--log N]
sb stop ID [--reason "..."]
sb adopt <unit|pid> --name NAME [--host H]
```

`sb run` records the job under the agent and project that launched it, chooses a host, and launches it there as a
transient user unit `sb-job-<id>` in `switchboard-jobs.slice`:

- placement: hosts in `order`; a host qualifies when free threads, free RAM and (for `--gpu`) free VRAM on one card
  cover the request after the host's reserve and the holds of running jobs. A card that drives a display is used
  only when nothing else fits (`display_gpu: avoid`) or never (`never`). Per-project `budgets` on threads, memory,
  VRAM and job count apply first. Nothing fits means `queued`, with the reason, retried every probe.
- bounds: `CPUWeight` (the slice's; 20 on a desktop host, 100 elsewhere), `MemoryMax`, `RuntimeMaxSec`,
  `KillMode=control-group`, `Nice=10`, and `ConditionPathExists=!…/HALT` so a halt raised between placement and
  start leaves it unstarted (`refused`). The slice file is written on first use with `MemoryHigh` at the host's
  total minus its reserve.
- environment: `SB_JOB_ID`, `SB_AGENT`, `SB_PROJECT`, `SB_HOST`, and `CUDA_VISIBLE_DEVICES` for GPU jobs.
- tracking: states `queued`, `running`, `finished`, `failed` (exit code, past `--hours`, out of memory), `stopped`,
  `lost` (unit gone without a result), `refused`, `preempted`. The unit keeps its result until the hub has read it.
  `sb jobs show` tails its journal.
- `sb adopt` watches an existing unit without relaunching it; adopted units are never halted and finish when their
  unit leaves.

Heavy processes that did not come through `sb run` appear on the fleet view as **untracked**, attributed to the
agent terminal they descend from (on the hub's machine), else their unit, else their working directory.
`untracked_policy` is `flag` (show it), `nudge` (also message the agent once) or `stop`.

Every agent's launch note tells it to use `sb run` and that untracked work shows under its name.

## 4. Optional backends

`switchboard/placers.py` detects two external tools (`jobs.gpu_workboard`, `jobs.cpu_run`: `"auto"`, a path, or
`null`). Without them the hub's own admission above applies and nothing is lost.

- A GPU lease board with a CLI of the shape `submit / acquire / start / heartbeat / finish --json` (the hub was
  written against the Atlas GPU workboard): for `--gpu` jobs the hub submits and acquires on the target host,
  launches with the card the board assigned, registers the unit, renews the lease every probe tick and finishes it
  with the outcome. A refusal keeps the job queued with the board's reason; a board preemption becomes `preempted`.
- A CPU placement advisor whose dry run returns `{"status": "ready", "host": ..., "cpus": [...]}`: the hub places on
  that host when its own inventory agrees and pins the unit with `AllowedCPUs=`. Each host may carry `cpu_run_name`
  when the advisor knows it by another name.

## 5. Topology notes

A common shape is a Linux desktop, a Windows workstation running WSL, and a headless GPU box on one LAN. Two things
bite:

- A WSL instance stops when its last console closes and takes sshd and every tunnel inside it along.
  `sb fleet wsl-keepalive [DISTRO]` prints the Windows scheduled-task command that keeps it alive.
- A host only reachable inbound can be reached through a reverse ssh tunnel it opens to the hub's machine
  (`ssh -NT -R PORT:127.0.0.1:22 hub-host` in a user unit with `Restart=always`), with a matching alias in the
  hub machine's `~/.ssh/config`.

## Tests

`python3 -m unittest tests.test_hosts tests.test_jobs tests.test_placers`. The remote paths run through
`tests/fake_ssh.py` (no network); the backends through `tests/fake_workboard.py` and `tests/fake_cpu_run.py`;
launch tests need a systemd user manager and are skipped without one. Tests use their own `SB_STATE` and
`SB_SYSTEMD_USER_DIR`, so they never raise a real HALT or touch real units.
