"""`keenyspace doctor` — read-only diagnostics.

Checks: server /healthz, server /readyz, auth validity, config sanity,
auth.json mode, daemon socket, dropped events, filesystem perms.

Output: rich.Table by default; --json switches to a machine-readable list of
{name, status, detail} dicts.
"""

from __future__ import annotations

import json as _json
import os
import stat
import sys
from dataclasses import dataclass

import httpx
from rich.console import Console
from rich.table import Table

from keenyspace.auth import read_auth
from keenyspace.config import get_client_settings
from keenyspace.paths import (
    AUTH_JSON,
    CONFIG_DIR,
    CONFIG_YAML,
    DAEMON_SOCK,
    DEFAULT_PULL_ROOT,
    DROPPED_JSON,
    STATE_DIR,
)


@dataclass
class CheckResult:
    name: str
    status: str  # ok | warn | fail
    detail: str


_STATUS_COLOR = {"ok": "green", "warn": "yellow", "fail": "red"}


async def _check_health(client: httpx.AsyncClient) -> CheckResult:
    try:
        resp = await client.get("/healthz")
    except httpx.RequestError as exc:
        return CheckResult(
            "server /healthz", "fail", f"unreachable: {type(exc).__name__}"
        )
    if resp.status_code == 200:
        return CheckResult("server /healthz", "ok", "HTTP 200")
    return CheckResult("server /healthz", "fail", f"HTTP {resp.status_code}")


async def _check_ready(client: httpx.AsyncClient) -> CheckResult:
    try:
        resp = await client.get("/readyz")
    except httpx.RequestError as exc:
        return CheckResult(
            "server /readyz", "fail", f"unreachable: {type(exc).__name__}"
        )
    if resp.status_code == 200:
        return CheckResult("server /readyz", "ok", "HTTP 200")
    return CheckResult("server /readyz", "warn", f"HTTP {resp.status_code}")


async def _check_auth_validity(
    client: httpx.AsyncClient, token: str | None
) -> CheckResult:
    if not token:
        return CheckResult(
            "auth validity",
            "warn",
            "no token in auth.json (run `keenyspace login`)",
        )
    try:
        resp = await client.get("/v1/api/auth/api-keys")
    except httpx.RequestError as exc:
        return CheckResult("auth validity", "fail", str(type(exc).__name__))
    if resp.status_code == 200:
        return CheckResult("auth validity", "ok", "HTTP 200")
    return CheckResult("auth validity", "fail", f"HTTP {resp.status_code}")


def _check_config_sanity() -> CheckResult:
    try:
        settings = get_client_settings()
        _ = settings.server_url
        _ = settings.default_workspace
    except Exception as exc:
        return CheckResult("config sanity", "fail", str(exc))
    return CheckResult("config sanity", "ok", str(CONFIG_YAML))


def _check_auth_file_mode() -> CheckResult:
    if sys.platform == "win32":
        return CheckResult("auth.json mode", "warn", "non-Unix host")
    if not AUTH_JSON.exists():
        return CheckResult("auth.json mode", "warn", "auth.json missing")
    mode = stat.S_IMODE(AUTH_JSON.stat().st_mode)
    if mode == 0o600:
        return CheckResult("auth.json mode", "ok", oct(mode))
    return CheckResult("auth.json mode", "fail", oct(mode))


def _check_daemon_socket() -> CheckResult:
    if DAEMON_SOCK.exists():
        return CheckResult("daemon socket", "ok", f"present at {DAEMON_SOCK}")
    return CheckResult(
        "daemon socket",
        "warn",
        f"missing at {DAEMON_SOCK} (run `keenyspace service install`)",
    )


def _check_dropped_events() -> CheckResult:
    if not DROPPED_JSON.exists():
        return CheckResult("dropped events", "ok", "no drops recorded")
    try:
        data = _json.loads(DROPPED_JSON.read_text())
        by_kind = data.get("by_kind", {})
        total = sum(int(b.get("count", 0)) for b in by_kind.values())
    except (OSError, ValueError, TypeError):
        return CheckResult("dropped events", "warn", "unreadable")
    if total == 0:
        return CheckResult("dropped events", "ok", "total=0")
    return CheckResult("dropped events", "warn", f"total={total}")


def _check_fs_perms() -> CheckResult:
    problems: list[str] = []
    for d in (CONFIG_DIR, STATE_DIR, DEFAULT_PULL_ROOT):
        if d.exists() and not os.access(d, os.W_OK):
            problems.append(str(d))
    if not problems:
        return CheckResult("filesystem perms", "ok", "writable")
    return CheckResult("filesystem perms", "fail", f"unwritable: {problems}")


async def run_doctor(as_json: bool = False) -> None:
    settings = get_client_settings()
    auth = read_auth() if AUTH_JSON.exists() else {}
    token = auth.get("api_key") or auth.get("access_token")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    results: list[CheckResult] = []
    async with httpx.AsyncClient(
        base_url=settings.server_url,
        timeout=5.0,
        headers=headers,
    ) as client:
        results.append(await _check_health(client))
        results.append(await _check_ready(client))
        results.append(await _check_auth_validity(client, token))
    results.append(_check_config_sanity())
    results.append(_check_auth_file_mode())
    results.append(_check_daemon_socket())
    results.append(_check_dropped_events())
    results.append(_check_fs_perms())

    if as_json:
        # Pure stdout JSON; sys.stdout.write avoids rich.Console formatting noise
        sys.stdout.write(
            _json.dumps(
                [
                    {"name": r.name, "status": r.status, "detail": r.detail}
                    for r in results
                ],
                indent=2,
            )
        )
        sys.stdout.write("\n")
        return

    console = Console()
    table = Table(title="keenyspace doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for r in results:
        color = _STATUS_COLOR[r.status]
        table.add_row(r.name, f"[{color}]{r.status}[/{color}]", r.detail)
    console.print(table)
