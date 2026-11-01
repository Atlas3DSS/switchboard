# Switchboard on your phone

The hub only listens on `127.0.0.1`. To use it from your phone, start the **phone gate** and reach it over HTTPS.
The gate lets exactly one paired phone in; everyone else sees a "not paired" page and nothing more.

```bash
./install.sh --phone      # adds the gate as a user service on 127.0.0.1:8810
```

(Without systemd: `python3 -m switchboard.gate` from the repo folder.)

## Option 1: Tailscale (simplest, nothing public)

1. Install [Tailscale](https://tailscale.com/download) on this computer and on your phone, signed in to the same
   tailnet.
2. Serve the gate over HTTPS inside your tailnet:

   ```bash
   tailscale serve --bg --https=443 http://127.0.0.1:8810
   ```

3. Your phone address is the `https://<machine>.<tailnet>.ts.net` name that command prints.

Nothing is reachable from the internet. Tailscale's servers coordinate the connection, and your traffic goes
directly between your devices.

## Option 2: your own domain

Use this if you'd rather own every piece. You need a hostname you control and a TLS reverse proxy in front of the gate.

**Proxy on this computer.** Forward TCP 443 on your router to this computer and run [Caddy](https://caddyserver.com)
here:

```
sb.example.com {
    reverse_proxy 127.0.0.1:8810
}
```

**Proxy on a server, no open port at home.** Run Caddy on a small server with a public IP
(`reverse_proxy 127.0.0.1:18150`). Then carry the traffic home over a reverse SSH tunnel that this computer
keeps open:

1. Make a key that is only for the tunnel: `ssh-keygen -t ed25519 -f ~/.ssh/switchboard_gate_tunnel -N ""`.
2. On the server, allow that key to open one listening port and nothing else, in the tunnel user's
   `~/.ssh/authorized_keys`:

   ```
   restrict,port-forwarding,permitlisten="127.0.0.1:18150",command="/bin/false" ssh-ed25519 AAAA... switchboard-gate-tunnel
   ```

3. On this computer, keep the tunnel up with a user unit (`~/.config/systemd/user/switchboard-gate-tunnel.service`):

   ```
   [Unit]
   Description=Switchboard phone gate tunnel
   After=network-online.target

   [Service]
   ExecStart=/usr/bin/ssh -NT -i %h/.ssh/switchboard_gate_tunnel -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes \
     -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:18150:127.0.0.1:8810 user@your-server
   Restart=always
   RestartSec=15

   [Install]
   WantedBy=default.target
   ```

   Then: `systemctl --user daemon-reload && systemctl --user enable --now switchboard-gate-tunnel`.

Either way, point your hostname's DNS `A` record at the machine that runs Caddy. Caddy gets the certificate for you.

## Pair the phone

Open Switchboard on this computer and click **Phone** in the top bar. Enter your phone address (`https://...`) the
first time, then click **Pair a phone**. Scan the QR code with the phone's camera within 10 minutes.

On iPhone, tap Share → **Add to Home Screen** and open Switchboard from there afterwards. Safari deletes stored
data for sites you haven't visited in a week, and that includes the phone's key. Home Screen apps are exempt.

## What "only that phone" means

- Pairing creates a private key in that phone's browser that cannot be exported. Every sign-in signs a fresh
  challenge with it. A copied link, an already-used code, or another browser gets the "not paired" page.
- One phone at a time: pairing a new phone signs the old one out at once. So does **Unpair** in the Phone dialog.
- Another browser on the same phone, a private tab, or clearing the site's data means pairing again.
- Pairing codes can only be made on this computer. The gate never passes `/api/phone` through.
- The gate stores only hashes of codes and sessions, in `~/.local/state/switchboard/phone.json` (mode 0600).
  Sessions last 24 hours and are renewed silently with the key.
- After 20 failed pairing or sign-in attempts in ten minutes, the gate turns away new attempts for a while.
