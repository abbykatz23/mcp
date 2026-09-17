# pi-mcp-server

A personal MCP server for your Raspberry Pi: status/logs/start-stop-restart
for `mbta-display`, `kindle-web`, and `n8n`, plus control of the Divoom
Pixoo display, reachable from Claude (web, mobile, desktop) over
`https://mcp.pre-idea.com`, restricted to your Google account.

## What's in here

- `server.py` -- the MCP server (FastMCP, Python)
- `requirements.txt`
- `.env.example` -- copy to `.env` and fill in
- `pi-mcp-server.service` -- systemd unit to run it on boot
- `sudoers-pi-mcp-server` -- narrow sudo rule for the two systemd units

## 1. Get the code onto the Pi

```bash
mkdir -p ~/pi-mcp-server
# copy server.py, requirements.txt, .env.example, pi-mcp-server.service,
# sudoers-pi-mcp-server into ~/pi-mcp-server
cd ~/pi-mcp-server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Docker permission for the n8n tools

If your user isn't already in the `docker` group:

```bash
sudo usermod -aG docker abbykatz
# log out and back in for this to take effect
```

## 3. Install sshpass (for the remote Pi status check)

```bash
sudo apt install sshpass
```

## 4. Install the sudoers rule (for mbta-display / kindle-web control)

```bash
which systemctl   # confirm the path -- edit sudoers-pi-mcp-server if it's not /usr/bin/systemctl
sudo visudo -cf sudoers-pi-mcp-server
sudo cp sudoers-pi-mcp-server /etc/sudoers.d/pi-mcp-server
sudo chmod 440 /etc/sudoers.d/pi-mcp-server
```

## 5. Create a Google OAuth client

This is what lets claude.ai authenticate you (and only you) to the server.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) →
   create a new project (or reuse one).
2. **APIs & Services → OAuth consent screen**: choose "External," fill in
   the minimal required fields, and add your own Google account under
   **Test users**. Leave the app in "Testing" status -- since only you'll
   ever use it, you don't need to submit it for Google's verification
   review.
3. **APIs & Services → Credentials → Create Credentials → OAuth client
   ID** → Application type "Web application."
4. Under **Authorized redirect URIs**, add:
   `https://mcp.pre-idea.com/auth/callback`
5. Save, and copy the generated Client ID and Client Secret.

## 6. Configure

```bash
cp .env.example .env
chmod 600 .env
```

Fill in `.env`:
- `MCP_OWNER_EMAIL` -- your Google account email (this is the actual
  access control -- anyone else who completes Google login is still
  rejected inside every tool)
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` -- from step 5
- `PIXOO_IP` -- defaults to `10.0.0.212`, change if it's moved
- `PIXOO_PAUSE_FLAG_PATH` -- defaults to `/tmp/pixoo_pause.flag`; must
  match `PIXOO_PAUSE_FLAG_PATH` in mbta-display's own `.env` (check
  there first -- this only matches if it hasn't been overridden)
- `MCP_AUDIT_LOG` -- defaults to a file in this directory
- `REMOTE_PI_HOST` / `REMOTE_PI_USER` / `REMOTE_PI_PASSWORD` -- login for
  the second Pi's read-only status check. Requires `sshpass` installed
  on this Pi (`sudo apt install sshpass`), since it's a password login
  rather than a key
- `REMOTE_PI_SERVICES` -- comma-separated allow-list of systemd unit
  names on that Pi that `get_remote_service_status` may query. Find
  names by SSHing in yourself and running
  `systemctl list-units --type=service --state=running`

## 7. Expose it via your existing Cloudflare Tunnel

Same pattern as `n8n.pre-idea.com`. Add an ingress rule mapping
`mcp.pre-idea.com` to `http://localhost:8000`, e.g. in your tunnel's
`config.yml`:

```yaml
ingress:
  - hostname: mcp.pre-idea.com
    service: http://localhost:8000
  - hostname: n8n.pre-idea.com
    service: http://localhost:5678
  - service: http_status:404
```

Then add the corresponding DNS record (`cloudflared tunnel route dns
<tunnel-name> mcp.pre-idea.com`) and restart the tunnel.

## 8. Install and start the service

```bash
sudo cp pi-mcp-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-mcp-server
journalctl -u pi-mcp-server -f
```

You should see FastMCP start up and bind to `127.0.0.1:8000`. Leave the
`journalctl -f` running while you do step 9, so you can see what happens
on the first connection attempt.

## 9. Add it to Claude

In claude.ai: **Settings → Connectors → Add custom connector**, and enter:

```
https://mcp.pre-idea.com/mcp
```

(FastMCP serves the MCP endpoint at `/mcp` by default under Streamable
HTTP -- if this 404s, check the startup log for the actual path.) Click
Connect, sign in with the Google account you listed as
`MCP_OWNER_EMAIL`, and it should come back "Connected" with the tool list
visible. Do the same in the Claude mobile and desktop apps if you want it
there too (connectors are per-app).

**Heads up:** claude.ai's OAuth handling for self-hosted custom
connectors has had real rough edges reported through 2026 (failed token
exchanges, redirect URI mismatches, etc.) -- if the connect step fails,
the error message + the `journalctl -u pi-mcp-server -f` output together
usually point at the problem (most commonly: a redirect URI that doesn't
exactly match what's in `allowed_client_redirect_uris` in `server.py` or
in the Google Cloud Console). It's worth trying it early rather than
building on top of it and discovering an auth issue later.

## Tools this exposes

| Tool | Does |
|---|---|
| `get_service_status(service)` | status for `mbta-display`, `kindle-web`, or `n8n` |
| `get_service_logs(service, lines, since, priority)` | journalctl / docker logs |
| `control_service(service, action)` | start / stop / restart |
| `get_remote_service_status(service?)` | systemd status for oli's e-ink display Pi (`oli-web` by default), over SSH |
| `pixoo_take_over_display()` | pause mbta-display (touches flag, waits ~22s) before changing the Pixoo |
| `pixoo_release_display()` | resume mbta-display (removes flag); also the manual recovery tool if a takeover was never released |
| `pixoo_get_channel()` | current Pixoo channel |
| `pixoo_set_channel(channel)` | switch channel (3 = trains, 0/1/2 = built-in) |
| `pixoo_get_device_config()` | full Pixoo device config |
| `pixoo_get_clock_info()` | clock-face info (unreliable per device notes) |
| `pixoo_set_clock_face(clock_id)` | set clock face by ID (untested IDs) |
| `get_recent_activity(limit)` | tail of the audit log |

Every tool call is appended to the audit log (`MCP_AUDIT_LOG`) with a
timestamp, the arguments, and the outcome.

## Extending it later

Adding a new controllable service means adding one entry to the
`SERVICES` dict in `server.py` -- everything else (status, logs, control)
is generic over that dict already. Anything not in that dict is
unreachable, by design; resist the urge to add a general-purpose "run a
shell command" tool.
