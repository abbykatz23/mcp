#!/usr/bin/env python3
"""
Personal MCP server for Abby's Raspberry Pi.

Exposes, to a single authorized Google account:
  - status / logs / start-stop-restart for: mbta-display, kindle-web
    (systemd units) and n8n (Docker container)
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

AUDIT_LOG_PATH = Path(
    os.environ.get("MCP_AUDIT_LOG", str(Path(__file__).parent / "activity.log"))
)

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
