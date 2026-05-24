"""`keenyspace status` — server + user + workspace + daemon panel."""

from __future__ import annotations

import socket

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from keenyspace.auth import read_auth
from keenyspace.clients.http import build_http_client
from keenyspace.config import get_client_settings
from keenyspace.paths import AUTH_JSON, DAEMON_SOCK


async def _probe_healthz(client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get("/healthz")
        return f"{resp.status_code}"
    except httpx.RequestError as exc:
        return f"unreachable: {type(exc).__name__}"


async def _probe_identity(client: httpx.AsyncClient) -> str:
    # No /v1/api/auth/me endpoint on the server (Phase 3 — see 05-02-SUMMARY);
    # GET /v1/api/auth/api-keys returns 200 if authed, 401 otherwise.
    try:
        resp = await client.get("/v1/api/auth/api-keys")
    except httpx.RequestError as exc:
        return f"unreachable: {type(exc).__name__}"
    if resp.status_code == 200:
        auth_payload = read_auth()
        token = auth_payload.get("access_token") or auth_payload.get("api_key")
        if token and token.startswith("ks_live_"):
            return f"api-key {token[:12]}..."
        return "authenticated"
    if resp.status_code == 401:
        return "not authenticated (401)"
    return f"unexpected status {resp.status_code}"


def _probe_daemon_socket() -> str:
    if not DAEMON_SOCK.exists():
        return "(missing)"
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        sock.connect(str(DAEMON_SOCK))
        sock.close()
    except OSError as exc:
        return f"(error: {type(exc).__name__})"
    return "(reachable)"


async def run_status() -> None:
    console = Console()
    settings = get_client_settings()
    async with build_http_client(timeout=5.0) as client:
        healthz = await _probe_healthz(client)
        identity = await _probe_identity(client)

    auth_present = AUTH_JSON.exists()
    table = Table(show_header=False, box=None)
    table.add_row("Server URL", settings.server_url)
    table.add_row("Server /healthz", healthz)
    table.add_row("User", identity)
    table.add_row("Default workspace", settings.default_workspace or "(none)")
    table.add_row(
        "Auth file",
        f"{AUTH_JSON} {'' if auth_present else '(missing)'}".strip(),
    )
    table.add_row("Daemon socket", f"{DAEMON_SOCK} {_probe_daemon_socket()}".strip())
    console.print(Panel(table, title="keenyspace status"))
