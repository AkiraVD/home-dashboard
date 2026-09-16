# home-dashboard

Live resource monitor, web terminal and Claude Code launcher for one Linux machine, reachable only from your tailnet:

**https://&lt;your-node&gt;.&lt;your-tailnet&gt;.ts.net/**

Paths below are from the machine it was built on (`/media/akiravd/Data/app/home-dashboard`, uid 1000); adjust them to yours.

- **Overview:** CPU, memory, GPU, temperatures, disks, network and battery with 1 hour of history (in memory, reset on restart); **Tailscale sites** (everything published with `tailscale serve`: link, local target, owning process/container, tailnet-only vs Funnel, and an up/down check that sends `GET /` to each local backend every 30 s while the dashboard is open); a process table (sortable, filterable, groupable by app); Docker containers; failed and running systemd units; listening ports. Updates every 2 s.
- **Claude:** the folders from `~/.claude/projects` (real paths recovered from the session files), with every running `claude` process of yours. **Start** asks for a Remote Control session name and whether to continue the last conversation, then opens a gnome-terminal window on the laptop running `claude [--continue] --remote-control <name>`; work on it from claude.ai. **Close** hangs up that terminal session, which stops Claude and closes the window. The list is deliberately not on a timer — it reloads when you open the tab, after a start or close, and when you press Refresh — so nothing moves while you type; an open start form keeps its values across a refresh. Cards are sorted alphabetically, never by activity. Tailscale identity + same-origin only, no TOTP (it starts and stops sessions, it doesn't change anything).
- **Terminal:** a real login shell (`bash -l` as `akiravd`) in the browser. `sudo` prompts for your password as usual. Closing the tab or losing the connection ends the shell and everything started in it. There's an on-screen key bar (Esc, Tab, Ctrl, Alt, arrows) on touch devices.

## Access and security

| Layer | What it does |
|---|---|
| Unix socket | The app listens on `$XDG_RUNTIME_DIR/home-dashboard/dash.sock` (mode 0600 in a 0700 dir). No TCP port; only this user and tailscaled can connect. |
| Tailscale Serve | Publishes the socket on the base link (`:443`), HTTPS, tailnet only. |
| Identity | Every request must carry Serve's `Tailscale-User-Login` = `HD_ALLOWED_LOGIN`; anything else gets 403. |
| Origin check | WebSockets and POSTs must come from an address where Serve publishes this dashboard (read from `tailscale serve status`, so moving it to another port needs no restart). Blocks cross-site WebSocket hijacking. |
| TOTP | The terminal needs a 6-digit code; it unlocks that device for 12 h (HMAC-signed, `Secure; HttpOnly; SameSite=Strict` cookie bound to login + device IP). 5 wrong codes → 15 min lockout. |
| Idle timeout | A shell with no input or output for 15 min is closed. |
| Audit log | `~/.local/state/home-dashboard/audit.log`: unlocks, failed codes, lockouts, session open/close with device. Keystrokes are never logged. |
| CSP | Scripts only from the app itself; all data is inserted as text, never HTML. |

## Setup

```bash
cd /media/akiravd/Data/app/home-dashboard
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# service
cp home-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now home-dashboard

# publish on the tailnet (persists across reboots). Serving a Unix socket needs
# root even for the Tailscale operator user.
sudo tailscale serve --bg --https=443 unix:/run/user/1000/home-dashboard/dash.sock
```

### TOTP (do this yourself, in a terminal window on the laptop)

```bash
./venv/bin/python -m app.totp_setup             # shows a QR code, asks for a code to confirm
./venv/bin/python -m app.totp_setup --check 123456
./venv/bin/python -m app.totp_setup --force     # new key; signs out every device
```

The key lives in `~/.config/home-dashboard/totp.key` (0600).

## Operating

```bash
systemctl --user status home-dashboard
journalctl --user -u home-dashboard -f
tail -f ~/.local/state/home-dashboard/audit.log
tailscale serve status
sudo tailscale serve --https=443 off    # unpublish
```

Restarting the service (including from inside the web terminal) ends every open web-terminal shell.

## Notes

- **GPU:** NVML is opened only for each reading, never while the GPU is runtime-suspended, and polled every 30 s while idle, so it doesn't keep an on-demand GPU awake. (On this machine the driver has runtime D3 disabled, so the GPU is always on anyway.)
- **Process CPU %** is per core, like `top` (100% = one full core).
- **Ports:** processes owned by other users (root services) show a PID-less row without root.
- **Cost:** the always-on sampler reads CPU/memory/disk/network/battery/GPU every 2 s. Processes, Docker, services and ports are only collected while a dashboard is open.

## Layout

```
app/
  __main__.py     binds the Unix socket, runs uvicorn
  main.py         routes, Tailscale identity gate, security headers
  auth.py         identity, origin check, TOTP gate, unlock cookie
  hub.py          2 s sampler, 1 h history, WebSocket fan-out
  collectors.py   CPU/mem/GPU/disks/net/battery, processes, Docker, systemd, ports
  terminal.py     pty + login shell <-> WebSocket, idle timeout, session cleanup
  audit.py        JSON-lines audit log
  totp_setup.py   one-time TOTP enrolment
static/
  index.html app.css app.js term.js theme.js
  vendor/         xterm.js 6.0.0, @xterm/addon-fit 0.11.0, uPlot 1.6.32
```
