"""httpx.AsyncClient factory pre-populated with server_url + Bearer token."""

from __future__ import annotations

import httpx

from keenyspace.auth import read_auth
from keenyspace.config import get_client_settings


def build_http_client(timeout: float = 30.0) -> httpx.AsyncClient:
    settings = get_client_settings()
    auth_payload = read_auth()
    token = auth_payload.get("access_token") or auth_payload.get("api_key")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        base_url=settings.server_url,
        headers=headers,
        timeout=timeout,
    )


async def build_authed_http_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Like build_http_client, but first ensures a valid bearer.

    Silently refreshes a stale OIDC access token (or runs device login when no
    refresh is possible) so an action command never 401s on an expired token.
    ks_live_ API keys pass through untouched. Use build_http_client directly for
    read-only/diagnostic paths (e.g. status) that must not trigger a login.
    """
    from keenyspace.cli.login import ensure_token

    await ensure_token()
    return build_http_client(timeout=timeout)
