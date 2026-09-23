"""A long-running MCP-like session on an API key must never start 401-ing.

Driven with ASGITransport + freezegun rather than a real uvicorn subprocess: the server
runs single-worker, so at the auth layer ASGITransport is equivalent and gives fast,
deterministic feedback.

API keys carry no expiry by design — the verify path only checks revoked_at IS NULL and
a lookup_hash match. These tests pin that contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_1h_api_key_session_no_401(app, _engine_lifespan_ctx, api_key_user) -> None:
    """A one-hour session on an API key never 401s.

    Thirteen calls at 5-minute steps over 60 minutes of simulated wall-clock, each an
    authenticated call to `/v1/api/auth/api-keys` (representative of an endpoint behind
    the composite backend plus the refresh dependency). A 401 mid-session would mean the
    key expired by accident, or the refresh dependency took a false-positive
    auto-refresh path.
    """
    _, plaintext = api_key_user
    headers = {"Authorization": f"Bearer {plaintext}"}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    start = datetime(2026, 5, 20, 0, 0, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
            for minute in range(0, 65, 5):  # 0, 5, 10, ..., 60
                frozen.move_to(start + timedelta(minutes=minute))
                resp = await c.get("/v1/api/auth/api-keys")
                assert resp.status_code != 401, (
                    f"401 at minute {minute} — API key expired mid-session "
                    f"(body: {resp.text[:200]})"
                )
                assert resp.status_code == 200, (
                    f"unexpected {resp.status_code} at minute {minute}: {resp.text[:200]}"
                )


@pytest.mark.asyncio
async def test_api_key_path_is_idp_independent(app, _engine_lifespan_ctx, api_key_user) -> None:
    """Sanity: the API-key path resolves without depending on the IdP.

    The resolver order is cookie → api_key → oidc_bearer. With no ks_at in the request
    the cookie path returns None and the api_key path resolves the User through
    `ApiKeyService.verify` without touching OidcClient — so an IdP that could not serve
    JWKS would still leave this call at 200.
    """
    _, plaintext = api_key_user
    headers = {"Authorization": f"Bearer {plaintext}"}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        resp = await c.get("/v1/api/auth/api-keys")
    assert resp.status_code == 200, resp.text
