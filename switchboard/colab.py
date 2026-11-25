"""Google Colab compute units on the Antigravity account card.

Read from the Colab CLI the owner is signed in to on this machine:

  colab usage      Current balance: 3026.50 compute units / Usage rate: 30.15/hr / Active assignments: 3
  colab sessions   [name] machine-id | Hardware: G4 | Shape: Standard | Variant: GPU     (one line per session)

One reading is kept TTL_S and refreshed on a thread, so the account cards never wait on the CLI. With no CLI, or one
that is signed out or answers something else, there is no reading and the card shows no line.

"changed_at" is when the balance last moved (Google bills in ticks about five minutes apart, so a
still number needs its age beside it). It is kept in SEEN across restarts; with nothing kept, it is taken from the
CLI's own debug log, which records every balance the CLI has been given.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

TTL_S = 180
BIN = os.path.expanduser("~/.local/bin/colab")
SEEN = os.path.join(os.path.expanduser(os.environ.get("SB_STATE", "~/.local/state/switchboard")), "colab_seen.json")
CLI_LOG = os.path.expanduser("~/.config/colab-cli/colab.log")
_state = {"at": 0.0, "got": None, "busy": False}
_lock = threading.Lock()


def from_log(balance, path=None):
    """Epoch of the first reading in the CLI's log since which the balance has been `balance`, or None."""
    first = stamp = None
    try:
        with open(path or CLI_LOG, errors="replace") as f:
            for line in f:
                m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
                if m:
                    stamp = m.group(1)
                b = re.search(r'"currentBalance":\s*([\d.]+)', line)
                if b and stamp:
                    if round(float(b.group(1)), 2) != round(balance, 2):
                        first = None
                    elif first is None:
                        first = stamp
        return time.mktime(time.strptime(first, "%Y-%m-%d %H:%M:%S")) if first else None
    except (OSError, ValueError):
        return None


def changed_at(balance, now, seen=None, log=None):
    """When the balance last moved (epoch, or None when not known), remembering this reading in SEEN."""
    seen = seen or SEEN
    try:
        kept = json.load(open(seen))
    except (OSError, ValueError):
        kept = {}
    if "balance" not in kept:
        at = from_log(balance, log)
    elif round(kept["balance"], 2) != round(balance, 2):
        at = now
    else:
        return kept.get("changed_at")
    try:
        os.makedirs(os.path.dirname(seen), exist_ok=True)
        with open(seen + ".tmp", "w") as f:
            json.dump({"balance": balance, "changed_at": at}, f)
        os.replace(seen + ".tmp", seen)
    except OSError:
        pass
    return at


def parse(usage_text, sessions_text=""):
    """-> {"balance", "rate", "assignments", "sessions": [{"name", "hardware"}], "runway_h"} or None."""
    bal = re.search(r"Current balance:\s*([\d,.]+)", usage_text or "")
    if not bal:
        return None
    rate = re.search(r"Usage rate:\s*([\d,.]+)\s*/\s*hr", usage_text)
    n = re.search(r"Active assignments:\s*(\d+)", usage_text)
    num = lambda m: float(m.group(1).replace(",", "")) if m else 0.0
    out = {"balance": num(bal), "rate": num(rate), "assignments": int(n.group(1)) if n else 0, "sessions": []}
    for m in re.finditer(r"^\[([^\]]+)\][^|\n]*\|\s*Hardware:\s*([^|\n]+)", sessions_text or "", re.M):
        out["sessions"].append({"name": m.group(1).strip(), "hardware": m.group(2).strip()})
    out["runway_h"] = round(out["balance"] / out["rate"], 1) if out["rate"] > 0 else None
    return out


def _run(*args):
    exe = BIN if os.path.exists(BIN) else shutil.which("colab")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _refresh(run, seen=None, log=None):
    try:
        text = run("usage")
        got = parse(text, run("sessions") if text else "")
        if got:
            got["changed_at"] = changed_at(got["balance"], time.time(), seen, log)
    except Exception:
        got = None
    with _lock:
        _state.update(at=time.time(), got=got, busy=False)


def reading(now=None, run=None, wait=False, seen=None, log=None):
    """The last reading, with "read_at" (epoch); None when there is none. Starts a refresh when it is TTL_S old."""
    now = time.time() if now is None else now
    with _lock:
        due = now - _state["at"] >= TTL_S and not _state["busy"]
        if due:
            _state["busy"] = True
    if due:
        if wait:
            _refresh(run or _run, seen, log)
        else:
            threading.Thread(target=_refresh, args=(run or _run, seen, log), daemon=True).start()
    with _lock:
        return dict(_state["got"], read_at=_state["at"]) if _state["got"] else None
