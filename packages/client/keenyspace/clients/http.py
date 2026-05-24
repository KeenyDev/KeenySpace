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
