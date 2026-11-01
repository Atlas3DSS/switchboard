#!/usr/bin/env bash
# Install Switchboard: `sb` on your PATH and the hub as a systemd user service on 127.0.0.1:8802. Safe to re-run.
#   ./install.sh            install / update and start the service
#   ./install.sh --no-service   only put `sb` on PATH (run the hub yourself: python3 -m switchboard.hub)
#   ./install.sh --phone        also start the phone gate on 127.0.0.1:8810 (see docs/PHONE.md)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
port=${SB_PORT:-8802}
phone=0
for arg in "$@"; do [ "$arg" = "--phone" ] && phone=1; done

command -v python3 >/dev/null || { echo "Switchboard needs python3 (3.10 or newer)." >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Switchboard needs Python 3.10 or newer." >&2; exit 1; }

mkdir -p ~/.local/bin
ln -sf "${here}/bin/sb" ~/.local/bin/sb
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: add ~/.local/bin to your PATH to use 'sb' from any shell." ;; esac

if [ "${1:-}" = "--no-service" ] || ! command -v systemctl >/dev/null || ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "Start the hub with:  cd ${here} && python3 -m switchboard.hub"
  echo "Then open http://127.0.0.1:${port}/ (or run 'sb setup' to answer the setup questions in the terminal)."
  exit 0
fi

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/switchboard.service <<UNIT
[Unit]
Description=Switchboard hub (projects + agents, 127.0.0.1:${port})

[Service]
WorkingDirectory=${here}
ExecStart=$(command -v python3) -m switchboard.hub
Environment=SB_PORT=${port}
# Your shell's PATH at install time, so the service finds the same claude / codex / gemini you do.
Environment="PATH=${here}/bin:${HOME}/.local/bin:${PATH}"
Restart=always
RestartSec=3
# Agent terminals run in their own processes and must survive a hub restart; the hub reconnects to them.
KillMode=process

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now switchboard.service
systemctl --user restart switchboard.service
for _ in $(seq 20); do curl -fs "http://127.0.0.1:${port}/api/overview" >/dev/null 2>&1 && break; sleep 0.5; done
if [ "${phone}" = 1 ]; then
  cat > ~/.config/systemd/user/switchboard-gate.service <<UNIT
[Unit]
Description=Switchboard phone gate (127.0.0.1:8810 -> hub; only the paired phone gets through)
After=switchboard.service

[Service]
WorkingDirectory=${here}
ExecStart=$(command -v python3) -m switchboard.gate
Environment=SB_PORT=${port}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now switchboard-gate.service
  systemctl --user restart switchboard-gate.service
  echo "Phone gate: 127.0.0.1:8810. Next: put HTTPS in front of it and pair your phone (docs/PHONE.md)."
fi
if systemctl --user is-active --quiet switchboard; then
  echo "Switchboard is running: http://127.0.0.1:${port}/"
  echo "First time? The page opens on Setup (or run 'sb setup' here)."
  echo "Keep it running after you log out:  loginctl enable-linger \$USER"
else
  echo "The service did not start; see: journalctl --user -u switchboard -n 50" >&2
  exit 1
fi
