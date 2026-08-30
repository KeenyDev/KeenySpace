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
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from rich.console import Console

# Module-attribute access (not from-imports): tests reload keenyspace.paths /
# keenyspace.auth / keenyspace.config per case, and a from-import here would
# freeze pre-reload path constants for every caller that goes through login.
from keenyspace import auth as auth_file
from keenyspace import config as client_config
from keenyspace import paths

CLIENT_ID = "keenyspace-cli"
SCOPES = "openid profile email groups"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT = "refresh_token"
# Refresh (or re-login) when the access token has under this many seconds left,
# so a long-running command does not 401 mid-flight.
REFRESH_THRESHOLD_SECONDS = 60


async def _discover_authentik_issuer(client: httpx.AsyncClient, server_url: str) -> str:
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


async def _discover_device_endpoints(client: httpx.AsyncClient, issuer: str) -> tuple[str, str]:
    """Return (device_authorization_endpoint, token_endpoint) from the IdP's
    OIDC discovery document.

    These endpoints must be READ from the discovery doc, not constructed by
    appending to the issuer: Authentik serves the per-application issuer at
    ``/application/o/<slug>`` but exposes device/token at the host root
    (``/application/o/device/``, ``/application/o/token/``). Appending the
    path to the issuer doubles it and yields a 405.
    """
    resp = await client.get(f"{issuer}/.well-known/openid-configuration")
    resp.raise_for_status()
    data = resp.json()
    device_endpoint = data.get("device_authorization_endpoint")
    token_endpoint = data.get("token_endpoint")
    if not isinstance(device_endpoint, str) or not isinstance(token_endpoint, str):
        raise RuntimeError(
            "IdP OIDC config is missing device_authorization_endpoint or "
            f"token_endpoint (issuer={issuer})."
        )
    return device_endpoint, token_endpoint


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
    except ValueError, KeyError, json.JSONDecodeError:
        return "(unknown)"


def _access_token_exp(access_token: str) -> int | None:
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except ValueError, KeyError, json.JSONDecodeError:
        return None


def _access_token_fresh(access_token: str) -> bool:
    exp = _access_token_exp(access_token)
    if exp is None:
        return False
    return (exp - time.time()) > REFRESH_THRESHOLD_SECONDS


async def _refresh_tokens(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Silently exchange the stored refresh_token for a fresh access token.

    Returns the IdP token payload on success, or None if no refresh_token /
    issuer is stored or the IdP rejects the grant (expired/rotated refresh
    token) — the caller then falls back to interactive device login.
    """
    refresh_token = payload.get("refresh_token")
    issuer = payload.get("issuer")
    if not isinstance(refresh_token, str) or not isinstance(issuer, str) or not issuer:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            disco = await client.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
            disco.raise_for_status()
            token_endpoint = disco.json().get("token_endpoint")
            if not isinstance(token_endpoint, str):
                return None
            resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": REFRESH_GRANT,
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
            )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    body = resp.json()
    if not isinstance(body, dict) or not body.get("access_token"):
        return None
    return body


async def ensure_token(*, interactive: bool = True) -> str | None:
    """Return a usable bearer for a CLI command, refreshing/logging in if stale.

    - ``ks_live_*`` API keys never expire -> returned as-is.
    - A fresh OIDC access token -> returned as-is.
    - A stale access token -> silent refresh via refresh_token; if that is
      unavailable or rejected, fall back to interactive device login.
    - No stored credentials at all -> None (callers direct the user to
      `keenyspace login`).

    ``interactive=False`` (headless contexts, e.g. the daemon) stops before the
    device-flow fallback and returns None instead of blocking on a browser login.
    """
    payload = auth_file.read_auth()
    api_key = payload.get("api_key")
    if isinstance(api_key, str) and api_key:
        return api_key

    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        # Never logged in: don't spring a device flow on an arbitrary command;
        # the explicit first step is `keenyspace login`. The interactive
        # fallback below is only for an expired session that failed to refresh.
        return None
    if _access_token_fresh(access):
        return access

    refreshed = await _refresh_tokens(payload)
    if refreshed is not None:
        issuer = payload.get("issuer")
        auth_file.write_auth(
            {
                "access_token": refreshed["access_token"],
                "refresh_token": (refreshed.get("refresh_token") or payload.get("refresh_token")),
                "expires_in": refreshed.get("expires_in"),
                "obtained_at": datetime.now(UTC).isoformat(),
                "issuer": issuer,
            }
        )
        token: str = refreshed["access_token"]
        return token

    if not interactive:
        return None

    await run_login(server_url=None)
    new_token = auth_file.read_auth().get("access_token")
    return new_token if isinstance(new_token, str) else None


async def run_login(server_url: str | None) -> None:
    console = Console()
    effective_url = (server_url or client_config.get_client_settings().server_url).rstrip("/")

    async with httpx.AsyncClient(timeout=30.0) as client:
        authentik_base = await _discover_authentik_issuer(client, effective_url)
        device_endpoint, token_endpoint = await _discover_device_endpoints(client, authentik_base)

        device_resp = await client.post(
            device_endpoint,
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

        loop = asyncio.get_running_loop()
        deadline = loop.time() + expires_in
        while loop.time() < deadline:
            await asyncio.sleep(interval)
            tok_resp = await client.post(
                token_endpoint,
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
    auth_file.write_auth(
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


async def run_login_pat(token: str, server_url: str | None) -> None:
    """Persist a personal access token (``ks_live_*``) as durable, non-expiring auth.

    The alternate login type to the device-code flow: no IdP round-trip, no
    expiry, no refresh. ensure_token() returns a stored ``api_key`` verbatim, so
    this is the right credential for headless contexts (e.g. the daemon). The
    token is validated against the server before it is written.
    """
    console = Console()
    err = Console(stderr=True)
    token = token.strip()
    if not token:
        err.print("[red]Empty token.[/red]")
        raise SystemExit(2)
    effective_url = (server_url or client_config.get_client_settings().server_url).rstrip("/")
    # Listing api-keys requires authentication, so a 2xx proves the token is
    # accepted; 401 means rejected/revoked.
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{effective_url}/v1/api/auth/api-keys",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        err.print(f"[red]Could not reach server to validate token: {exc}[/red]")
        raise SystemExit(2) from exc
    if resp.status_code == 401:
        err.print("[red]Token rejected (401). Check it is a valid, unrevoked key.[/red]")
        raise SystemExit(2)
    if resp.status_code >= 400:
        err.print(f"[red]Token validation failed (HTTP {resp.status_code}).[/red]")
        raise SystemExit(2)
    auth_file.write_auth({"api_key": token, "obtained_at": datetime.now(UTC).isoformat()})
    if not auth_file.is_api_key(token):
        console.print(
            "[yellow]Note: token does not start with ks_live_; stored as a bearer anyway.[/yellow]"
        )
    console.print("[green]Token saved[/green] (durable, non-expiring; auth.json mode 0600).")


async def run_token_create(name: str, server_url: str | None) -> None:
    """Mint a personal access token via the current (device-flow) login."""
    console = Console()
    err = Console(stderr=True)
    effective_url = (server_url or client_config.get_client_settings().server_url).rstrip("/")
    api_key = await ensure_token()
    if not api_key:
        err.print("[red]Not logged in. Run `keenyspace login` first.[/red]")
        raise SystemExit(2)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{effective_url}/v1/api/auth/api-keys",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"name": name},
            )
    except httpx.HTTPError as exc:
        err.print(f"[red]Mint request failed: {exc}[/red]")
        raise SystemExit(2) from exc
    if resp.status_code >= 400:
        err.print(f"[red]Mint failed (HTTP {resp.status_code}): {resp.text}[/red]")
        raise SystemExit(2)
    key = resp.json().get("key")
    console.print("[green]Personal access token created[/green] (shown once — save it now):")
    console.print(f"[bold]{key}[/bold]")
    console.print("Use it as a durable login: `keenyspace login --pat` then paste it.")


async def run_logout() -> None:
    console = Console()
    if not paths.AUTH_JSON.exists():
        console.print("Already logged out")
        return
    payload = auth_file.read_auth()
    token = payload.get("access_token") or payload.get("api_key")
    server_url = client_config.get_client_settings().server_url.rstrip("/")
    if token:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{server_url}/v1/api/auth/logout",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.RequestError:
            pass
    auth_file.clear_auth()
    console.print("[green]Logged out[/green]")


def _login_payload_for_test(access_token: str, issuer: str) -> dict[str, Any]:
    return {
        "access_token": access_token,
        "obtained_at": datetime.now(UTC).isoformat(),
        "issuer": issuer,
    }
