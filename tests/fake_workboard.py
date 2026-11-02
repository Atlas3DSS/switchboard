#!/usr/bin/env python3
"""A stand-in for gpu-workboard in tests. Logs every call (one JSON line per call) to $FAKE_WB_LOG and answers:
submit -> {"id": "gpu-fake-N"}; acquire -> exit 0 with an assigned card, or exit 3 and a decision reason when
$FAKE_WB_DENY is set; start/heartbeat/finish/cancel -> ok; status -> the jobs seen so far, with the status from
$FAKE_WB_STATUS (default "running") and one fake card."""
import json
import os
import sys

log = os.environ.get("FAKE_WB_LOG") or "/dev/null"
args = sys.argv[1:]
cmd = args[0] if args else ""
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
if cmd == "submit":
    n = sum(1 for l in open(log) if l.startswith('["submit"')) if log != "/dev/null" else 1
    print(json.dumps({"id": f"gpu-fake-{n}", "status": "queued"}))
elif cmd == "acquire":
    if os.environ.get("FAKE_WB_DENY"):
        print(json.dumps({"acquired": False, "decision_reason": os.environ["FAKE_WB_DENY"]}))
        sys.exit(3)
    print(json.dumps({"acquired": True, "assigned_uuid": os.environ.get("FAKE_WB_UUID", "GPU-FAKE-0001")}))
elif cmd in ("start", "heartbeat", "finish", "cancel"):
    print(json.dumps({"ok": True}))
elif cmd == "status":
    seen = []
    if log != "/dev/null":
        for l in open(log):
            a = json.loads(l)
            if a and a[0] == "submit":
                seen.append(f"gpu-fake-{len(seen) + 1}")
    print(json.dumps({"schema": "fake", "coordinator": "here", "observed_at": "now",
                      "fleet": {"cards": [{"host": "here", "label": "fake-card", "uuid": "GPU-FAKE-0001",
                                           "free_mib": 20000, "fresh": True, "active_job_ids": seen}]},
                      "jobs": [{"id": s, "label": "x", "project": "alpha", "status": os.environ.get("FAKE_WB_STATUS", "running"),
                                "lane": "batch", "target_host": "here", "assigned_uuid": "GPU-FAKE-0001",
                                "decision_reason": os.environ.get("FAKE_WB_REASON", "")} for s in seen]}))
else:
    sys.exit(f"fake_workboard: unknown command {cmd}")
