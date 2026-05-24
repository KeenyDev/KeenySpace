"""`keenyspace login` — Authentik device-code flow (RFC 8628) + logout.

Phase 3 D-14: CLI hits Authentik directly for the device flow; the
KeenySpace server is involved only to discover the IdP issuer (via
/v1/api/auth/discovery if it exists; falls back to .well-known on the
server URL; final fallback is the env var KEENYSPACE_AUTHENTIK_ISSUER).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from rich.console import Console

from keenyspace.auth import clear_auth, read_auth, write_auth
from keenyspace.config import get_client_settings
from keenyspace.paths import AUTH_JSON

CLIENT_ID = "keenyspace-cli"
SCOPES = "openid profile email groups"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


async def _discover_authentik_issuer(
    client: httpx.AsyncClient, server_url: str
) -> str:
    # 1. Try a server-side discovery shim (Phase 3 may add /v1/api/auth/discovery).
    try:
        resp = await client.get(f"{server_url}/v1/api/auth/discovery")
        if resp.status_code == 200:
            data = resp.json()
            issuer = data.get("issuer")
            if isinstance(issuer, str) and issuer:
                return issuer.rstrip("/")
    except httpx.RequestError:
        pass
    # 2. Fall back to OIDC well-known on the server itself.
    try:
        resp = await client.get(f"{server_url}/.well-known/openid-configuration")
        if resp.status_code == 200:
            data = resp.json()
            issuer = data.get("issuer")
            if isinstance(issuer, str) and issuer:
                return issuer.rstrip("/")
    except httpx.RequestError:
        pass
    # 3. Env-var escape hatch for self-host operators with a separate IdP host.
    env_issuer = os.environ.get("KEENYSPACE_AUTHENTIK_ISSUER")
    if env_issuer:
        return env_issuer.rstrip("/")
    raise RuntimeError(
        "Could not discover Authentik issuer URL. "
        "Tried /v1/api/auth/discovery and /.well-known/openid-configuration. "
        "Set KEENYSPACE_AUTHENTIK_ISSUER to override."
    )


def _decode_sub(access_token: str) -> str:
    # JWT middle segment, base64url-decoded. We do NOT validate signature here;
    # the server validates on every API call (Pitfall #4: token audience is
    # the server's concern, see Phase 7 docs).
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return "(unknown)"
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        sub = payload.get("sub")
        return str(sub) if sub else "(unknown)"
    except (ValueError, KeyError, json.JSONDecodeError):
        return "(unknown)"


async def run_login(server_url: str | None) -> None:
    console = Console()
    effective_url = (server_url or get_client_settings().server_url).rstrip("/")

    async with httpx.AsyncClient(timeout=30.0) as client:
        authentik_base = await _discover_authentik_issuer(client, effective_url)

        device_resp = await client.post(
            f"{authentik_base}/application/o/device/",
            data={"client_id": CLIENT_ID, "scope": SCOPES},
        )
        device_resp.raise_for_status()
        device_payload = device_resp.json()
        device_code = device_payload["device_code"]
        user_code = device_payload["user_code"]
        verification_uri_complete = device_payload.get(
            "verification_uri_complete", device_payload.get("verification_uri", "")
        )
        expires_in = int(device_payload.get("expires_in", 600))
        interval = int(device_payload.get("interval", 5))

        console.print("[cyan]Open this URL in a browser:[/cyan]")
        console.print(f"[bold]{verification_uri_complete}[/bold]")
        console.print(f"User code: [yellow]{user_code}[/yellow]")

        loop = asyncio.get_event_loop()
        deadline = loop.time() + expires_in
        while loop.time() < deadline:
            await asyncio.sleep(interval)
            tok_resp = await client.post(
                f"{authentik_base}/application/o/token/",
                data={
                    "grant_type": DEVICE_GRANT,
                    "device_code": device_code,
                    "client_id": CLIENT_ID,
                },
            )
            if tok_resp.status_code == 200:
                token_payload = tok_resp.json()
                break
            body = tok_resp.json()
            err = body.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err in ("access_denied", "expired_token"):
                raise RuntimeError(f"Login failed: {err}")
            raise RuntimeError(f"Login failed: unexpected response {body}")
        else:
            raise TimeoutError("Device code expired before authentication")

    access_token = token_payload["access_token"]
    # Pitfall #4: we persist the access_token bytes verbatim. Audience (aud)
    # invariant is enforced by the KeenySpace server's AuthMiddleware on every
    # API call (Phase 3 D-14, Phase 7 docs). Client does NOT validate aud.
    write_auth(
        {
            "access_token": access_token,
            "refresh_token": token_payload.get("refresh_token"),
            "expires_in": token_payload.get("expires_in"),
            "obtained_at": datetime.now(UTC).isoformat(),
            "issuer": authentik_base,
        }
    )

    sub = _decode_sub(access_token)
    console.print("[green]Login successful[/green]")
    console.print(f"Logged in as {sub}")


async def run_logout() -> None:
    console = Console()
    if not AUTH_JSON.exists():
        console.print("Already logged out")
        return
    payload = read_auth()
    token = payload.get("access_token") or payload.get("api_key")
    server_url = get_client_settings().server_url.rstrip("/")
    if token:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{server_url}/v1/api/auth/logout",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.RequestError:
            pass
    clear_auth()
    console.print("[green]Logged out[/green]")


def _login_payload_for_test(access_token: str, issuer: str) -> dict[str, Any]:
    return {
        "access_token": access_token,
        "obtained_at": datetime.now(UTC).isoformat(),
        "issuer": issuer,
    }
