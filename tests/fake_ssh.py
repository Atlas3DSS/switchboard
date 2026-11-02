#!/usr/bin/env python3
"""A stand-in for ssh in tests: drops the options and the host, runs the remote command locally with the same
stdin, so the hub's remote code paths (probe over `python3 -`, scripts over `bash -s`) run without a network.
A host named `unreachable` fails the way ssh does when a host is down."""
import subprocess
import sys

args = sys.argv[1:]
while args and args[0].startswith("-"):
    args = args[2:] if args[0] in ("-o", "-p", "-i", "-l") else args[1:]
if not args:
    sys.exit("fake_ssh: no host")
host, cmd = args[0], " ".join(args[1:])
if host == "unreachable":
    sys.stderr.write("ssh: connect to host unreachable port 22: No route to host\n")
    sys.exit(255)
sys.exit(subprocess.call(["bash", "-c", cmd]))
