"""MCP-10 + AUTH-09 + Success Criterion #3.

1h MCP-like session с API key — НИКОГДА не 401.
Стратегия: ASGITransport + freezegun (без real subprocess uvicorn —
быстрый детерминированный feedback; PROJECT.md «Single-worker uvicorn v1»
делает ASGITransport архитектурно эквивалентным subprocess на уровне auth).

API keys по дизайну (D-13 + AUTH-03) не имеют exp — verify path просто
проверяет revoked_at IS NULL и lookup_hash match. Этот тест pin'ит контракт.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_1h_api_key_session_no_401(app, _engine_lifespan_ctx, api_key_user) -> None:
    """Success Criterion #3: 1-hour session с API key — никогда 401.

    12 итераций по 5 мин = 60 мин simulated wall-clock; на каждой делаем
    authed call к `/v1/api/auth/api-keys` (representative endpoint behind
    composite backend + refresh_dep dependency). Любой 401 на mid-session
    итерации = regression (API key expired by accident, or refresh_dep
    triggered false-positive auto-refresh path).
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
    """D-17 + Success Criterion #3 sanity: API-key path работает независимо от IdP.

    CompositeAuthBackend resolver order = cookie → api_key → oidc_bearer.
    Cookie path возвращает None (нет ks_at в request), api_key path резолвит
    User через `ApiKeyService.verify` БЕЗ touching OidcClient. Тест pin'ит:
    даже если бы OidcClient не мог fetch'нуть JWKS, api_key path всё равно
    отдаёт 200.
    """
    _, plaintext = api_key_user
    headers = {"Authorization": f"Bearer {plaintext}"}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        resp = await c.get("/v1/api/auth/api-keys")
    assert resp.status_code == 200, resp.text
