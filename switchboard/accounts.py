"""Token limits per account, read from an optional usage cache (SB_USAGE, default ~/.cache/switchboard/usage.json).

Switchboard does not fetch limits itself. If you have a tool that knows them, have it write this file and the account
cards, the limits panel and automatic account handoff use it; without the file nothing is shown and handoff only acts
when a CLI reports a limit on screen. Format:

  {"plans": {"<account name>": {"tier": "max", "read_at": <epoch seconds>,
             "limits": [{"label": "5h" | "wk" | "<model>", "used": <percent>, "resets": <epoch seconds>}]}}}

SB_USAGE_POLL names a command the hub runs every two minutes to refresh the file. Read-only otherwise."""
import json
import os
import shlex
import subprocess
import time

from . import discovery
from .store import iso_from_epoch

USAGE = os.path.expanduser(os.environ.get("SB_USAGE") or "~/.cache/switchboard/usage.json")
STALE_S = 2 * 3600
QUOTA_FRESH_S = 20 * 60


LABELS = {"5h": ("session", "Session (5h)"), "wk": ("weekly", "Weekly"), "Fable": ("fable", "Fable weekly")}


def usage():
    """Per account: each token limit as % used and when it resets (a window past its reset reads 0%)."""
    try:
        cache = json.load(open(USAGE)).get("plans", {})
    except (OSError, ValueError):
        cache = {}
    now = time.time()
    out = {}
    for a in discovery.accounts():
        e = cache.get(a["name"], {})
        limits = []
        for l in e.get("limits", []):
            key, label = LABELS.get(l.get("label"), (str(l.get("label")).lower(), str(l.get("label"))))
            past = bool(l.get("resets")) and l["resets"] < now
            limits.append({"key": key, "label": label,
                           "used": 0 if past else max(0, min(100, int(round(l.get("used") or 0)))),
                           "resets": iso_from_epoch(l["resets"]) if l.get("resets") and not past else None})
        order = {"session": 0, "weekly": 1, "fable": 2}
        limits.sort(key=lambda l: order.get(l["key"], 9))
        read_at = e.get("read_at") or e.get("fetched")
        if a["kind"] == "antigravity":  # what agy reports is left, and the last time its quota ran out
            from . import antigravity
            hit = antigravity.quota_hit(a.get("config_dir"))
            # what is left, as agy itself reports it (`agy -p "/usage"`): the Gemini group is this account's own
            # session and weekly limit. The subscription has a second pair for Claude and GPT models run through
            # agy; no agent here uses them, so that pair is shown only once something has been used from it.
            subs, sub_at = antigravity.subscription_usage(a.get("config_dir"))
            for l in subs or []:
                own = "gemini" in l["group"].lower()
                if not own and l["left"] >= 100:
                    continue
                name = {"session": "Session (5h)", "weekly": "Weekly"}.get(l["window"], l["window"])
                limits.append({"key": l["window"] if own else "other_" + l["window"],
                               "label": name if own else f"{l['group']}: {name}",
                               "used": 100 - l["left"], "resets": l["resets"]})
            limits.sort(key=lambda l: {"session": 0, "weekly": 1}.get(l["key"], 9))
            if subs:
                read_at = sub_at
            if hit and hit["resets"] > now:
                out_now = now - hit["at"] < QUOTA_FRESH_S  # agents retry: a hit this recent means it is still out
                if out_now or not subs:  # with a live reading, an old failure says nothing the limits do not
                    limits.append({"key": "quota", "label": "Quota ran out "
                                   + time.strftime("%a %H:%M", time.localtime(hit["at"]))
                                   + ("" if out_now else " (no failure since)"),
                                   "used": 100 if out_now else None, "resets": iso_from_epoch(hit["resets"])})
                    read_at = now
        out[a["name"]] = {"tier": e.get("tier"), "limits": limits,
                          "read_at": iso_from_epoch(read_at) if read_at else None,
                          "stale": bool(read_at) and now - read_at > STALE_S, "error": e.get("error")}
        if a["kind"] == "antigravity":  # Colab compute units are bought on the same Google account (colab.py)
            from . import colab
            cu = colab.reading()
            if cu:
                out[a["name"]]["colab"] = dict(cu, read_at=iso_from_epoch(cu["read_at"]),
                                               changed_at=iso_from_epoch(cu["changed_at"]) if cu.get("changed_at") else None)
    return out


def poll():
    """Run SB_USAGE_POLL, if set, to refresh the cache."""
    cmd = os.environ.get("SB_USAGE_POLL")
    if not cmd:
        return
    try:
        subprocess.run(shlex.split(cmd), capture_output=True, timeout=90)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
