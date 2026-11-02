#!/usr/bin/env python3
"""A stand-in for cpu-run's dry run in tests: prints a ready decision for host $FAKE_CPU_HOST (default "here") with
CPUs $FAKE_CPU_CPUS (default "2,3"), or a refusal when $FAKE_CPU_REFUSE is set. Logs its argv to $FAKE_CPU_LOG."""
import json
import os
import sys

if os.environ.get("FAKE_CPU_LOG"):
    with open(os.environ["FAKE_CPU_LOG"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_CPU_REFUSE"):
    print(json.dumps({"status": "refused", "refusals": [{"host": "here", "reason": os.environ["FAKE_CPU_REFUSE"]}]}))
else:
    print(json.dumps({"status": "ready", "host": os.environ.get("FAKE_CPU_HOST", "here"),
                      "cpus": [int(c) for c in os.environ.get("FAKE_CPU_CPUS", "2,3").split(",")], "threads": 1}))
