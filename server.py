#!/usr/bin/env python3
"""
Personal MCP server for Abby's Raspberry Pi.

Exposes, to a single authorized Google account:
  - status / logs / start-stop-restart for: mbta-display, kindle-web
    (systemd units) and n8n (Docker container)
  - read-only systemd status for an allow-listed unit on a second Pi,
    over SSH (password login)
  - control of a Divoom Pixoo display over its local HTTP API
  - a JSONL audit log of every tool call, queryable via get_recent_activity

Auth: Google OAuth via FastMCP's GoogleProvider, restricted to
MCP_OWNER_EMAIL. Anyone else who completes the Google login is still
rejected inside every tool.

Run this directly on the Pi's host OS (not inside Docker) as a systemd
service -- see pi-mcp-server.service and README.md for the full setup,
including the narrowly-scoped sudoers rule this depends on for
start/stop/restart of the two systemd units.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal, Optional

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token

# ---------------------------------------------------------------------------
# Configuration (all from environment -- see .env.example)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("MCP_BASE_URL", "https://mcp.pre-idea.com")
OWNER_EMAIL = os.environ.get("MCP_OWNER_EMAIL")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

PIXOO_IP = os.environ.get("PIXOO_IP", "10.0.0.212")
PIXOO_URL = f"http://{PIXOO_IP}/post"

# mbta-display polls for this flag file once per ~20s loop iteration and
# backs off whenever it exists (see its main.py / settings.py). This must
# match PIXOO_PAUSE_FLAG_PATH in mbta-display's own .env -- the default
# below matches mbta-display's default, but if that's been overridden on
# the Pi, override it here too.
PIXOO_PAUSE_FLAG_PATH = Path(
    os.environ.get("PIXOO_PAUSE_FLAG_PATH", "/tmp/pixoo_pause.flag")
)
PIXOO_PAUSE_SLEEP_SECONDS = 22  # a bit over one full mbta-display poll cycle

AUDIT_LOG_PATH = Path(
    os.environ.get("MCP_AUDIT_LOG", str(Path(__file__).parent / "activity.log"))
)

# A second Pi on the same LAN, reachable over SSH with a username/password
# login (no key exchange set up). Only for read-only systemd status checks
# on an explicit allow-list of unit names below -- see get_remote_service_status.
REMOTE_PI_HOST = os.environ.get("REMOTE_PI_HOST", "10.0.0.236")
REMOTE_PI_USER = os.environ.get("REMOTE_PI_USER", "oli")
REMOTE_PI_PASSWORD = os.environ.get("REMOTE_PI_PASSWORD")
# Comma-separated list of systemd unit names on that Pi this server may
# query. Nothing outside this list is reachable no matter what a tool
# call asks for -- same principle as SERVICES above.
REMOTE_PI_SERVICES = [
    s.strip()
    for s in os.environ.get("REMOTE_PI_SERVICES", "oli-web").split(",")
    if s.strip()
]

# Every service this server is allowed to touch. Nothing outside this map
# is reachable no matter what a tool call asks for -- this is the whole
# point of not exposing a generic "run a command" tool.
SERVICES = {
    "mbta-display": {"kind": "systemd"},
    "kindle-web": {"kind": "systemd"},
    "n8n": {"kind": "docker"},
}
ServiceName = Literal["mbta-display", "kindle-web", "n8n"]
ALLOWED_ACTIONS = ("start", "stop", "restart")

if not OWNER_EMAIL:
    raise RuntimeError("MCP_OWNER_EMAIL must be set to your Google account email")
if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set")

# ---------------------------------------------------------------------------
# Auth: Google OAuth, restricted to one account
# ---------------------------------------------------------------------------

auth = GoogleProvider(
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    base_url=BASE_URL,
    required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
    # Only claude.ai's connector callback is allowed to complete the
    # OAuth dance against this server. Add another entry here if you
    # later see a different callback URL rejected (e.g. Claude Desktop
    # using its own).
    allowed_client_redirect_uris=[
        "https://claude.ai/api/mcp/auth_callback",
    ],
)

mcp = FastMCP(name="pi-home-control", auth=auth)


def _check_owner() -> None:
    """Reject anyone who isn't the one allowed Google account.

    GoogleProvider only proves someone completed a Google login -- it
    doesn't restrict *which* Google account. This is the actual
    single-user gate, and every tool below calls it first.
    """
    token = get_access_token()
    email = None
    if token is not None:
        email = token.claims.get("email") or token.claims.get("Email")
    if email != OWNER_EMAIL:
        raise ToolError("This server is only authorized for its owner's Google account.")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _audit(tool: str, args: dict, outcome: str, detail: str = "") -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": tool,
        "args": args,
        "outcome": outcome,
        "detail": detail[:2000],
    }
    try:
        with AUDIT_LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # never let logging break a real request


# ---------------------------------------------------------------------------
# Shell-out helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 15) -> dict:
    """Run a fixed argument list -- never a shell string -- and capture output."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def _require_service(service: str) -> dict:
    if service not in SERVICES:
        raise ToolError(f"Unknown service '{service}'. Allowed: {', '.join(SERVICES)}")
    return SERVICES[service]


# ---------------------------------------------------------------------------
# Service status / logs / control
# ---------------------------------------------------------------------------


@mcp.tool
def get_service_status(service: ServiceName) -> dict:
    """Get the current status of one of the managed services.

    mbta-display and kindle-web are systemd units; n8n is a Docker
    container. Returns raw command output.
    """
    _check_owner()
    info = _require_service(service)
    if info["kind"] == "systemd":
        result = _run(["systemctl", "status", service, "--no-pager"])
    else:
        result = _run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name=^{service}$",
                "--format",
                "{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.Names}}",
            ]
        )
        if not result["stdout"]:
            result["stdout"] = f"No container named '{service}' found."
    _audit("get_service_status", {"service": service}, "ok", result["stdout"][:200])
    return result


@mcp.tool
def get_service_logs(
    service: ServiceName,
    lines: int = 50,
    since: Optional[str] = None,
    priority: Optional[str] = None,
) -> dict:
    """Get recent logs for a managed service.

    lines: how many lines to return (default 50).
    since: optional time filter, e.g. "10 min ago" or "2026-09-16 08:00:00"
        (systemd units use journalctl's --since; n8n uses docker logs' --since).
    priority: optional systemd priority filter, e.g. "err" or "warning"
        (systemd units only; ignored for n8n).
    """
    _check_owner()
    info = _require_service(service)
    if info["kind"] == "systemd":
        cmd = ["journalctl", "-u", service, "-n", str(lines), "--no-pager"]
        if since:
            cmd += ["--since", since]
        if priority:
            cmd += ["-p", priority]
    else:
        cmd = ["docker", "logs", "--tail", str(lines), service]
        if since:
            cmd += ["--since", since]
    result = _run(cmd, timeout=20)
    _audit(
        "get_service_logs",
        {"service": service, "lines": lines, "since": since, "priority": priority},
        "ok",
    )
    return result


@mcp.tool
def control_service(service: ServiceName, action: Literal["start", "stop", "restart"]) -> dict:
    """Start, stop, or restart a managed service.

    mbta-display and kindle-web go through `sudo systemctl <action>`,
    permitted by a sudoers rule scoped to exactly these two units and
    these three actions -- nothing broader. n8n goes through the docker
    CLI directly (the service account must be in the docker group).
    """
    _check_owner()
    info = _require_service(service)
    if action not in ALLOWED_ACTIONS:
        raise ToolError(f"Unknown action '{action}'. Allowed: {', '.join(ALLOWED_ACTIONS)}")

    if info["kind"] == "systemd":
        cmd = ["sudo", "-n", "systemctl", action, service]
    else:
        cmd = ["docker", action, service]

    result = _run(cmd)
    outcome = "ok" if result["returncode"] == 0 else "error"
    _audit("control_service", {"service": service, "action": action}, outcome, result["stderr"])
    return result


# ---------------------------------------------------------------------------
# Remote status check (a second Pi, over SSH with a password login)
# ---------------------------------------------------------------------------


def _ssh_run(remote_cmd: list[str], timeout: int = 20) -> dict:
    """Run a fixed argument list on REMOTE_PI_HOST over SSH, via sshpass.

    Password goes in through the SSHPASS env var, not a -p flag or the
    command line, so it doesn't show up in `ps` output for other users
    on the box. Never builds a shell string -- remote_cmd is passed as
    a literal argv to the remote sshd, same discipline as _run().
    """
    if not REMOTE_PI_PASSWORD:
        raise ToolError("REMOTE_PI_PASSWORD is not configured.")
    if shutil.which("sshpass") is None:
        raise ToolError(
            "sshpass isn't installed on this host -- install it "
            "(e.g. `sudo apt install sshpass`) to use remote SSH tools."
        )
    cmd = [
        "sshpass",
        "-e",
        "ssh",
        "-o", "BatchMode=no",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        f"{REMOTE_PI_USER}@{REMOTE_PI_HOST}",
        "--",
        *remote_cmd,
    ]
    env = dict(os.environ, SSHPASS=REMOTE_PI_PASSWORD)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


@mcp.tool
def get_remote_service_status(service: str) -> dict:
    """Get systemd status for an allow-listed unit on the other Pi, over SSH.

    Connects as REMOTE_PI_USER@REMOTE_PI_HOST using a password login (no
    SSH key exchange configured for this). Only unit names listed in
    REMOTE_PI_SERVICES are reachable -- nothing else, no matter what
    string is passed here. Read-only: this runs `systemctl status`, it
    doesn't start/stop/restart anything on that machine.
    """
    _check_owner()
    if service not in REMOTE_PI_SERVICES:
        raise ToolError(
            f"Unknown remote service '{service}'. Allowed: "
            f"{', '.join(REMOTE_PI_SERVICES) or '(none configured -- set REMOTE_PI_SERVICES)'}"
        )
    result = _ssh_run(["systemctl", "status", service, "--no-pager"])
    outcome = "ok" if result["returncode"] == 0 else "error"
    _audit("get_remote_service_status", {"service": service}, outcome, result["stdout"][:200])
    return result


# ---------------------------------------------------------------------------
# Divoom Pixoo control (the display mbta-display drives)
# ---------------------------------------------------------------------------


def _pixoo_post(payload: dict) -> dict:
    try:
        resp = httpx.post(PIXOO_URL, json=payload, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise ToolError(f"Couldn't reach the Pixoo at {PIXOO_IP}: {exc}") from exc


@mcp.tool
def pixoo_take_over_display() -> dict:
    """Pause mbta-display before making any change to what's on the Pixoo.

    Call this FIRST, before pixoo_set_channel, pixoo_set_clock_face, or
    any other call that changes what's showing on the screen. mbta-display
    is a separate long-running service that redraws the Pixoo roughly
    every 20 seconds; if you change the display without pausing it first,
    it will overwrite your change on its next poll.

    This touches a flag file that mbta-display checks once per poll loop,
    then blocks for a bit over one full cycle (~22s) so mbta-display has
    actually noticed and stopped pushing before returning -- skipping
    that wait risks a race where your change lands right before an
    in-flight frame push and gets immediately overwritten.

    The flag is left in place when this returns, so mbta-display stays
    paused indefinitely -- across as many Pixoo calls as you want to
    make -- until you call pixoo_release_display(). That is the whole
    point: the change you're about to make is meant to persist on
    screen, not just flash briefly. Do NOT call pixoo_release_display()
    right after making your change as if this were a single atomic
    action -- only call it once the user explicitly asks to go back to
    the trains display (or asks you to undo/cancel the takeover). If
    the user's request was itself just "briefly show X", confirm with
    them whether they want it to stay or revert before releasing.

    The one exception is error recovery: if something fails partway
    through and you're abandoning the takeover entirely, do release so
    mbta-display doesn't stay stuck paused with no visible error. If
    you're ever unsure whether a previous take-over was released, call
    pixoo_release_display() -- it's a safe no-op if nothing is paused.
    As a last-resort manual fix, the flag file can simply be deleted
    directly on the Pi.
    """
    _check_owner()
    try:
        PIXOO_PAUSE_FLAG_PATH.touch()
    except OSError as exc:
        _audit("pixoo_take_over_display", {}, "error", str(exc))
        raise ToolError(f"Couldn't create pause flag at {PIXOO_PAUSE_FLAG_PATH}: {exc}") from exc
    time.sleep(PIXOO_PAUSE_SLEEP_SECONDS)
    _audit("pixoo_take_over_display", {}, "ok", f"flag={PIXOO_PAUSE_FLAG_PATH}")
    return {
        "status": "paused",
        "flag_path": str(PIXOO_PAUSE_FLAG_PATH),
        "message": (
            "mbta-display is paused and should have stopped pushing frames. "
            "Make your Pixoo change(s) now, then call pixoo_release_display() "
            "when done -- it stays paused indefinitely otherwise."
        ),
    }


@mcp.tool
def pixoo_release_display() -> dict:
    """Resume mbta-display after you're done changing the Pixoo.

    Removes the flag touched by pixoo_take_over_display(). Within one
    poll cycle (~20s) mbta-display will notice, force the Pixoo back to
    its own custom channel itself, and resume pushing trains -- you do
    NOT need to switch the channel back yourself first.

    Only call this when the user explicitly asks to go back to the
    trains display, asks you to undo/cancel a takeover, or you're
    abandoning a takeover after an error. Do NOT call this automatically
    right after making a change with pixoo_set_channel or
    pixoo_set_clock_face -- that would immediately undo the change you
    were just asked to make. If it's unclear whether the user wants the
    change to persist or was only a one-off "briefly show X", ask them
    rather than guessing.

    Safe to call even if nothing is currently paused (no-op). This is
    also the recovery tool if a previous take-over was never released --
    call it any time you're unsure, to be safe.
    """
    _check_owner()
    was_present = PIXOO_PAUSE_FLAG_PATH.exists()
    try:
        PIXOO_PAUSE_FLAG_PATH.unlink(missing_ok=True)
    except OSError as exc:
        _audit("pixoo_release_display", {}, "error", str(exc))
        raise ToolError(f"Couldn't remove pause flag at {PIXOO_PAUSE_FLAG_PATH}: {exc}") from exc
    _audit(
        "pixoo_release_display",
        {},
        "ok",
        f"flag={PIXOO_PAUSE_FLAG_PATH} was_present={was_present}",
    )
    return {
        "status": "released" if was_present else "was_not_paused",
        "flag_path": str(PIXOO_PAUSE_FLAG_PATH),
    }


@mcp.tool
def pixoo_get_channel() -> dict:
    """Get the Pixoo display's current channel.

    SelectIndex 3 is the custom channel (the MBTA train display);
    0, 1, 2 are the device's built-in channels.
    """
    _check_owner()
    result = _pixoo_post({"Command": "Channel/GetIndex"})
    _audit("pixoo_get_channel", {}, "ok", str(result))
    return result


@mcp.tool
def pixoo_set_channel(channel: int = 0) -> dict:
    """Set the Pixoo display's channel.

    channel: 3 = custom channel (MBTA trains); 0, 1, 2 = built-in Divoom
    channels. If asked to go to the "default", "built-in", or "divoom"
    channel with no number given, use 0.

    Call pixoo_take_over_display() first, or mbta-display will overwrite
    this within its next ~20s poll cycle. Do NOT call
    pixoo_release_display() right after this as if the two were one
    action -- leave the display paused so the change actually persists.
    Only release once the user explicitly asks to switch back to trains
    (releasing does that for you; no need to call pixoo_set_channel(3)
    yourself).
    """
    _check_owner()
    if channel not in (0, 1, 2, 3):
        raise ToolError("channel must be 0, 1, 2, or 3")
    result = _pixoo_post({"Command": "Channel/SetIndex", "SelectIndex": channel})
    _audit("pixoo_set_channel", {"channel": channel}, "ok", str(result))
    return result


@mcp.tool
def pixoo_get_device_config() -> dict:
    """Get the Pixoo's full device config (brightness, rotation, clock ID, etc.).

    Note: CurClockId in this response is known to be stale on this
    device -- it doesn't update when the clock face is changed via the
    app. Don't rely on it to detect which face is currently showing.
    """
    _check_owner()
    result = _pixoo_post({"Command": "Channel/GetAllConf"})
    _audit("pixoo_get_device_config", {}, "ok")
    return result


@mcp.tool
def pixoo_get_clock_info() -> dict:
    """Get clock-face-specific info from the Pixoo.

    Note: unreliable on this device for the same reason as CurClockId
    above -- treat it as informational only, not a source of truth for
    which face is currently active.
    """
    _check_owner()
    result = _pixoo_post({"Command": "Channel/GetClockInfo"})
    _audit("pixoo_get_clock_info", {}, "ok")
    return result


@mcp.tool
def pixoo_set_clock_face(clock_id: int) -> dict:
    """Set the Pixoo's clock face by ID.

    Clock IDs on this device haven't been fully verified end-to-end --
    finding a working ID may take trial and error (set one, then look
    at the device to see what changed).

    Call pixoo_take_over_display() first, or mbta-display will overwrite
    this within its next ~20s poll cycle. Do NOT call
    pixoo_release_display() right after this as if the two were one
    action -- leave the display paused so the change actually persists.
    Only release once the user explicitly asks to switch back to trains.
    """
    _check_owner()
    result = _pixoo_post({"Command": "Channel/SetClockSelectId", "ClockId": clock_id})
    _audit("pixoo_set_clock_face", {"clock_id": clock_id}, "ok", str(result))
    return result


# ---------------------------------------------------------------------------
# Audit log access
# ---------------------------------------------------------------------------


@mcp.tool
def get_recent_activity(limit: int = 20) -> list:
    """Show the most recent tool calls made through this MCP server, newest first."""
    _check_owner()
    if not AUDIT_LOG_PATH.exists():
        return []
    lines = AUDIT_LOG_PATH.read_text().strip().splitlines()
    entries = [json.loads(line) for line in lines[-limit:] if line]
    return list(reversed(entries))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Bind to localhost only -- Cloudflare Tunnel is what makes this
    # reachable at MCP_BASE_URL. Never bind this to 0.0.0.0 directly.
    mcp.run(transport="http", host="127.0.0.1", port=8000)
