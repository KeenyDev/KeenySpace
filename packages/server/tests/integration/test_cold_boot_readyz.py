"""AUTH-02 Success Criterion #5 + D-15 + Pitfall 12 + T-3-40.

`/readyz` остаётся 200 при unreachable Authentik. Discovery — lazy
(`Authlib.load_server_metadata` НЕ вызывается из `/readyz`). respx подтверждает
zero HTTP egress на oidc_issuer_url во время readiness probe.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def app_with_unreachable_authentik(fs_root, pg_url, monkeypatch):
    """App fixture с oidc_issuer_url, указывающим на NONEXISTENT host.

    Postgres reachable (pg_url из conftest), FS root writeable (tmp_path),
    IdP — заведомо мёртвый. /readyz должен возвращать 200, потому что D-15
    запрещает touch'ить IdP в health/readiness probe path.
    """
    monkeypatch.setenv("KEENYSPACE_DB__URL", pg_url)
    monkeypatch.setenv("KEENYSPACE_FS__ROOT", str(fs_root))
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_ISSUER_URL",
        "http://nonexistent-idp.invalid/application/o/dead/",
    )
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_CLIENT_ID", "x")
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_CLIENT_SECRET", "x")
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_REDIRECT_URI",
        "http://test/v1/api/auth/callback",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI",
        "http://test/",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__API_KEY_PEPPER",
        "test-pepper-32chars-padded-here!",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__SESSION_SECRET_KEY",
        "test-session-secret-32chars-pad!",
    )
    monkeypatch.setenv("KEENYSPACE_AUTH__COOKIE_SECURE", "false")
    monkeypatch.setenv("KEENYSPACE_AUTO_MIGRATE", "true")
    from keenyspace_server.config import get_settings

    get_settings.cache_clear()
    from keenyspace_server.db.session import engine_lifespan
    from keenyspace_server.main import build_app

    application = build_app()
    async with engine_lifespan(application):
        yield application


@pytest.mark.asyncio
async def test_readyz_green_when_idp_unreachable(
    app_with_unreachable_authentik,
) -> None:
    """Success Criterion #5: cold boot, /readyz 200 даже если Authentik dead."""
    transport = ASGITransport(
        app=app_with_unreachable_authentik, raise_app_exceptions=False
    )
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("status") in {"ready", "ok", "healthy"} or "checks" in body


@pytest.mark.asyncio
@respx.mock(assert_all_called=False)
async def test_readyz_makes_no_oidc_http_call(
    app_with_unreachable_authentik,
) -> None:
    """T-3-40 + Open Question #8 ratification.

    respx mocks любой HTTP request к oidc_issuer_url. /readyz НЕ должен
    trigger discovery (lazy) → respx route НЕ должен быть called.
    """
    route = respx.get(url__startswith="http://nonexistent-idp.invalid/").mock()
    transport = ASGITransport(
        app=app_with_unreachable_authentik, raise_app_exceptions=False
    )
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        for _ in range(5):
            resp = await c.get("/readyz")
            assert resp.status_code == 200
    assert not route.called, (
        "readyz triggered HTTP call to oidc_issuer_url — discovery is NOT lazy"
    )


@pytest.mark.asyncio
async def test_healthz_green_when_idp_unreachable(
    app_with_unreachable_authentik,
) -> None:
    """Sanity: /healthz (liveness) тоже green — independent of IdP."""
    transport = ASGITransport(
        app=app_with_unreachable_authentik, raise_app_exceptions=False
    )
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
