"""Readiness must not depend on the IdP being up.

`/readyz` stays 200 while Authentik is unreachable, because OIDC discovery is lazy:
`Authlib.load_server_metadata` is never called from the probe. respx confirms zero HTTP
egress to oidc_issuer_url for the duration of a readiness probe.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def app_with_unreachable_authentik(fs_root, pg_url, monkeypatch):
    """App whose oidc_issuer_url points at a host that does not exist.

    Postgres is reachable (pg_url from conftest) and the FS root is writeable (tmp_path);
    only the IdP is dead. /readyz must still answer 200, because the health and readiness
    probe paths are forbidden from touching the IdP at all.
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
    """Cold boot with Authentik dead: /readyz is still 200."""
    transport = ASGITransport(app=app_with_unreachable_authentik, raise_app_exceptions=False)
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
    """A readiness probe makes no HTTP call to the IdP.

    respx intercepts any request to oidc_issuer_url; because discovery is lazy, /readyz
    must never trigger it, so the respx route must stay uncalled.
    """
    route = respx.get(url__startswith="http://nonexistent-idp.invalid/").mock()
    transport = ASGITransport(app=app_with_unreachable_authentik, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        for _ in range(5):
            resp = await c.get("/readyz")
            assert resp.status_code == 200
    assert not route.called, "readyz triggered HTTP call to oidc_issuer_url — discovery is NOT lazy"


@pytest.mark.asyncio
async def test_healthz_green_when_idp_unreachable(
    app_with_unreachable_authentik,
) -> None:
    """Sanity: /healthz (liveness) is green too — independent of the IdP."""
    transport = ASGITransport(app=app_with_unreachable_authentik, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
